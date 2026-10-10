"""One-off, explicit operator action after approval to reuse these two providers.

Run as root on the deployment host. Only the two named provider credentials are
read from topic-remix; only encrypted copies are written to the new service.
No credentials are printed, sent to the browser, or copied into release files.
"""
import os
from pathlib import Path
import pwd
import sys

ROOT=Path('/opt/douyin-breakout/current')
sys.path.insert(0,str(ROOT))

def fields(path, allowed):
    result={}
    for line in path.read_text().splitlines():
        line=line.strip()
        if not line or line.startswith('#') or '=' not in line:continue
        key,value=line.split('=',1);key=key.strip()
        if key not in allowed:continue
        value=value.strip()
        if len(value)>=2 and value[0]==value[-1] and value[0] in ('"',"'"):value=value[1:-1]
        result[key]=value
    return result

if os.geteuid()!=0:raise SystemExit('Run as the approved deployment operator.')
config=fields(Path('/etc/douyin-breakout.env'),{'BREAKOUT_MASTER_KEY','BREAKOUT_PUBLIC_URL','BREAKOUT_DATA_DIR','BREAKOUT_DAILY_LIVE_JOBS','BREAKOUT_DAILY_USER_JOBS'})
for key,value in config.items():
    if key.startswith('BREAKOUT_'):os.environ[key]=value
source=Path('/home/ubuntu/topic-remix/.env.local')
source_fields=fields(source,{'DEEPSEEK_API_KEY','TIKHUB_API_KEY','TIKHUB_API_KEY_FILE'})
model=source_fields.get('DEEPSEEK_API_KEY','')
data=source_fields.get('TIKHUB_API_KEY','')
if not data and source_fields.get('TIKHUB_API_KEY_FILE'):
    reference=Path(source_fields['TIKHUB_API_KEY_FILE'].replace('~/', '/home/ubuntu/', 1))
    if not reference.is_absolute():reference=source.parent/reference
    data=reference.read_text().strip()
if not model or not data:raise SystemExit('An approved provider credential is missing; no changes were made.')
from webapp.settings import Settings
from webapp.vault import Secrets
vault=Secrets(Settings.from_env())
vault.update({'deepseek_key':model,'tikhub_key':data})
account=pwd.getpwnam('breakout')
os.chown(vault.folder,account.pw_uid,account.pw_gid)
os.chown(vault.path,account.pw_uid,account.pw_gid)
print('Both approved provider configurations saved encrypted. No credentials were printed.')
