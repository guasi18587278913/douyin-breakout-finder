import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit


@dataclass
class Settings:
    data_dir: Path
    public_url: str
    master_key: str
    dev: bool = False
    daily_live_jobs: int = 2
    daily_user_jobs: int = 2
    session_days: int = 7
    worker_enabled: bool = True

    def __post_init__(self):
        self.data_dir = Path(self.data_dir).resolve()
        self.public_url = self.public_url.rstrip('/')
        url = urlsplit(self.public_url)
        if url.path or url.query or url.fragment or url.username or url.password:
            raise ValueError('Public URL must be an origin.')
        if self.dev:
            if url.hostname not in ('127.0.0.1', 'localhost', 'testserver'):
                raise ValueError('Development mode is restricted to local addresses.')
        elif url.scheme != 'https':
            raise ValueError('Hosted production requires HTTPS.')
        if not 1 <= self.daily_live_jobs <= 20 or not 1 <= self.daily_user_jobs <= 20:
            raise ValueError('Daily job limits must be between 1 and 20.')
        from cryptography.fernet import Fernet
        Fernet(self.master_key.encode())
        self.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        if os.name != 'nt': self.data_dir.chmod(0o700)

    @classmethod
    def from_env(cls):
        return cls(Path(os.environ.get('BREAKOUT_DATA_DIR', '/var/lib/douyin-breakout')),
                   os.environ.get('BREAKOUT_PUBLIC_URL', 'https://breakout.zmtgzt.com'),
                   os.environ['BREAKOUT_MASTER_KEY'], os.environ.get('BREAKOUT_DEV_MODE') == '1',
                   int(os.environ.get('BREAKOUT_DAILY_LIVE_JOBS', '2')),
                   int(os.environ.get('BREAKOUT_DAILY_USER_JOBS', '2')))
