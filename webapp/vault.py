"""Only the application master key can decrypt server-side provider configuration."""
import json
import os
import threading
from cryptography.fernet import Fernet

ALLOWED={'tikhub_key','deepseek_key'}


class Secrets:
    def __init__(self, settings):
        self.folder=settings.data_dir/'private'
        self.folder.mkdir(exist_ok=True,mode=0o700)
        if os.name!='nt':self.folder.chmod(0o700)
        self.path=self.folder/'providers.enc'
        self.crypto=Fernet(settings.master_key.encode())
        self.lock=threading.RLock()

    def get(self):
        with self.lock:
            if not self.path.exists():return {}
            data=json.loads(self.crypto.decrypt(self.path.read_bytes()))
            if not isinstance(data,dict) or set(data)-ALLOWED:raise ValueError('Invalid provider configuration.')
            return data

    def status(self):
        data=self.get()
        return {key:bool(data.get(key)) for key in sorted(ALLOWED)}

    def update(self, fields):
        if not isinstance(fields,dict) or not fields or set(fields)-ALLOWED:raise ValueError('Invalid configuration fields.')
        if any(not isinstance(v,str) or not 8<=len(v)<=500 or any(ord(c)<33 or ord(c)>126 for c in v) for v in fields.values()):
            raise ValueError('Invalid credential format.')
        with self.lock:
            data=self.get();data.update(fields)
            encoded=self.crypto.encrypt(json.dumps(data,separators=(',',':')).encode())
            temp=self.path.with_suffix('.tmp')
            fd=os.open(str(temp),os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600)
            try:
                with os.fdopen(fd,'wb') as f:f.write(encoded);f.flush();os.fsync(f.fileno())
                os.replace(temp,self.path)
            finally:temp.unlink(missing_ok=True)
        return self.status()
