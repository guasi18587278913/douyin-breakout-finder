import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

# 开发模式没配封面搭子接入密钥时用的假值：真实封面搭子不认它，只用来连本机的假封面搭子。
DEV_FM_CLIENT_SECRET = 'dev-only-fake-fm-client-secret'


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
    # 封面搭子（登录与积分）的地址、接入密钥，以及一次真实研究扣多少积分。
    fm_base: str = 'https://fm.zmtgzt.com'
    fm_client_secret: str = field(default='', repr=False)
    research_credits: int = 5

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
        self.fm_base = self.fm_base.rstrip('/')
        fm = urlsplit(self.fm_base)
        if fm.path or fm.query or fm.fragment or fm.username or fm.password or not fm.hostname:
            raise ValueError('BREAKOUT_FM_BASE 只能写封面搭子的网站根地址，例如 https://fm.zmtgzt.com。')
        fm.port  # 端口写错时这里会报 ValueError
        # 正式环境只走 HTTPS；开发模式另外允许本机的假封面搭子。
        if fm.scheme != 'https' and not (self.dev and fm.scheme == 'http' and fm.hostname in ('127.0.0.1', 'localhost')):
            raise ValueError('BREAKOUT_FM_BASE 必须使用 HTTPS。')
        if not self.fm_client_secret:
            if not self.dev:
                raise ValueError('BREAKOUT_FM_CLIENT_SECRET 未配置：正式环境必须配置封面搭子的接入密钥。')
            self.fm_client_secret = DEV_FM_CLIENT_SECRET
        secret = self.fm_client_secret
        # 只检查格式，报错时不带出密钥本身。
        if not isinstance(secret, str) or not 16 <= len(secret) <= 256 or any(ord(c) < 33 or ord(c) > 126 for c in secret):
            raise ValueError('BREAKOUT_FM_CLIENT_SECRET 格式不对：需要 16–256 个可见 ASCII 字符，不能有空格。')
        if isinstance(self.research_credits, bool) or not isinstance(self.research_credits, int) or not 1 <= self.research_credits <= 100:
            raise ValueError('BREAKOUT_RESEARCH_CREDITS 需要是 1–100 的整数。')
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
                   int(os.environ.get('BREAKOUT_DAILY_USER_JOBS', '2')),
                   fm_base=os.environ.get('BREAKOUT_FM_BASE', 'https://fm.zmtgzt.com'),
                   fm_client_secret=os.environ.get('BREAKOUT_FM_CLIENT_SECRET', ''),
                   research_credits=int(os.environ.get('BREAKOUT_RESEARCH_CREDITS', '5')))
