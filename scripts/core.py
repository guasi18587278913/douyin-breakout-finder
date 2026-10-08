"""Portable state, request budget and deterministic comparison rules (Python 3.9+)."""
import copy
import hashlib
import json
import os
import re
import shutil
import statistics
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

VERSION = '0.2.0'
DAY = 86400
OPS = ('search', 'posts', 'stats', 'comments')

class StopRun(Exception):
    """An expected, actionable stop; do not automatically retry."""

class BudgetStop(StopRun):
    pass

class APIError(StopRun):
    pass

def now():
    return datetime.now(timezone.utc).isoformat()

def stamp(value):
    parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if parsed.tzinfo is None:
        raise ValueError('timestamp requires a timezone')
    return parsed.timestamp()

def number(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, str) and re.fullmatch(r'\d+', value):
        return int(value)
    return None

def money(value, name='amount'):
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise StopRun('%s must be a decimal USD amount' % name)
    if not result.is_finite() or result < 0:
        raise StopRun('%s must be a finite, non-negative USD amount' % name)
    return result

def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + '.tmp')
    try:
        with tmp.open('w', encoding='utf-8') as f:
            json.dump(value, f, ensure_ascii=False, indent=2, allow_nan=False)
            f.write('\n')
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)

def read_json(path, default=None):
    path = Path(path)
    if not path.exists():
        return copy.deepcopy(default)
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except (ValueError, UnicodeError) as e:
        raise StopRun('Invalid JSON in %s: %s' % (path.name, type(e).__name__))

def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(',', ':')).encode('utf-8')).hexdigest()

def default_config(topic, keywords, budget=None):
    return {
        'version': VERSION, 'provider': 'tikhub', 'topic': topic,
        'scope': 'Describe what counts as relevant before reviewing results.',
        'keywords': keywords, 'search_days': 180,
        'max_followers': 10000, 'min_ratio': 10,
        'baseline_count': 10, 'min_baseline_count': 6,
        'comparison_window_days': 90, 'min_age_days': 7,
        'max_candidates': 8, 'max_search_pages': 1, 'max_author_pages': 3,
        'max_requests': 60, 'budget_usd': budget,
        'pricing': {'checked_at': None, 'source_url': None,
                    'usd_per_request': {op: None for op in OPS}},
    }

def validate_config(c):
    def reject_secrets(obj):
        if isinstance(obj, dict):
            for k, v in obj.items():
                if str(k).lower() in {'api_key', 'authorization', 'password', 'access_token', 'cookie'}:
                    raise StopRun('Credentials must stay in environment variables, not in the task configuration.')
                reject_secrets(v)
        elif isinstance(obj, list):
            for v in obj:
                reject_secrets(v)
    reject_secrets(c)
    if c.get('provider') != 'tikhub':
        raise StopRun('This version supports TikHub direct access only.')
    if not isinstance(c.get('topic'), str) or not c['topic'].strip():
        raise StopRun('topic is required')
    ks = c.get('keywords')
    if not isinstance(ks, list) or not ks or len(ks) > 30:
        raise StopRun('Use 1 to 30 keywords.')
    if any(not isinstance(k, str) or not k.strip() or len(k) > 200 for k in ks):
        raise StopRun('Each keyword must contain 1 to 200 characters.')
    if len(set(ks)) != len(ks):
        raise StopRun('Remove duplicate keywords.')
    for key in ('max_followers', 'search_days', 'baseline_count', 'min_baseline_count',
                'comparison_window_days', 'min_age_days', 'max_candidates',
                'max_search_pages', 'max_author_pages', 'max_requests'):
        if isinstance(c.get(key), bool) or not isinstance(c.get(key), int) or c[key] < 1:
            raise StopRun('%s must be a positive integer' % key)
    if c['min_baseline_count'] > c['baseline_count'] or c['baseline_count'] > 50:
        raise StopRun('Require min_baseline_count <= baseline_count <= 50.')
    if money(c.get('min_ratio'), 'min_ratio') < 1:
        raise StopRun('min_ratio must be at least 1')
    if c.get('budget_usd') is not None:
        money(c['budget_usd'], 'budget_usd')
    return c

class Store:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.config = validate_config(read_json(self.root / 'config.json', {}))
        self.state = read_json(self.root / 'state.json')
        if not isinstance(self.state, dict):
            raise StopRun('Run init first; state.json is missing.')
        if self.state.get('config_hash') != digest(self.config):
            raise StopRun('Configuration changed. Create a new run to preserve the evidence and budget.')
        self.events = self._events()

    @classmethod
    def create(cls, root, config, as_of=None, demo=False):
        root = Path(root).resolve()
        if root.exists() and any(root.iterdir()):
            raise StopRun('Output directory must be empty. Existing files were not changed.')
        validate_config(config)
        root.mkdir(parents=True, exist_ok=True)
        atomic_json(root / 'config.json', config)
        atomic_json(root / 'state.json', {
            'version': VERSION, 'config_hash': digest(config), 'as_of': as_of or now(),
            'demo': demo, 'candidates': {}, 'queries': {}, 'authors': {}, 'stats': {},
            'results': {}, 'comments': {}, 'analyses': {}, 'stops': []})
        return cls(root)

    def _events(self):
        p = self.root / 'requests.jsonl'
        if not p.exists():
            return []
        result = []
        try:
            for line in p.read_text(encoding='utf-8').splitlines():
                if line.strip():
                    result.append(json.loads(line))
        except (ValueError, UnicodeError):
            raise StopRun('Request journal is incomplete. Preserve it; do not start more paid requests.')
        return result

    def save(self):
        atomic_json(self.root / 'state.json', self.state)

    def event(self, event):
        event = dict(event, at=now())
        with (self.root / 'requests.jsonl').open('a', encoding='utf-8') as f:
            f.write(json.dumps(event, ensure_ascii=False, allow_nan=False) + '\n')
            f.flush()
            os.fsync(f.fileno())
        self.events.append(event)

    def reservations(self):
        return [e for e in self.events if e['event'] == 'reserved']

    def reserved_cost(self):
        return sum((money(e['reserved_usd']) for e in self.reservations()), Decimal('0'))

    def reserve(self, op, key, retry=False):
        if shutil.disk_usage(self.root).free < 64 * 1024 * 1024:
            raise StopRun('Not enough free disk space to safely save a paid response.')
        reservations = self.reservations()
        if len(reservations) >= self.config['max_requests']:
            raise BudgetStop('Request limit reached.')
        cost = money(self.config['pricing']['usd_per_request'][op], op + ' unit price')
        limit = money(self.config['budget_usd'], 'budget_usd')
        if self.reserved_cost() + cost > limit:
            raise BudgetStop('USD budget would be exceeded.')
        self.event({'event': 'reserved', 'operation': op, 'key': key,
                    'reserved_usd': str(cost), 'retry': retry})

    @contextmanager
    def lock(self):
        path = self.root / '.run.lock'
        try:
            fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            raise StopRun('Another process or an interrupted run holds .run.lock. Check it before removing the lock.')
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as f:
                f.write(str(os.getpid()))
            # A second process may have loaded stale state before this lock was acquired.
            current = validate_config(read_json(self.root / 'config.json', {}))
            state = read_json(self.root / 'state.json')
            if not isinstance(state, dict) or state.get('config_hash') != digest(current):
                raise StopRun('Configuration changed during the run. Start a new run.')
            self.config, self.state, self.events = current, state, self._events()
            yield
        finally:
            path.unlink(missing_ok=True)

    def pricing_ready(self, operations):
        pricing = self.config.get('pricing', {})
        if self.config.get('budget_usd') is None:
            raise StopRun('Set an explicitly agreed budget_usd before paid use.')
        if not pricing.get('source_url') or not pricing.get('checked_at'):
            raise StopRun('Record current endpoint prices, their source_url and checked_at before paid use.')
        from urllib.parse import urlsplit
        url = urlsplit(pricing['source_url'])
        if url.scheme != 'https' or url.hostname not in {
            'tikhub.io', 'www.tikhub.io', 'docs.tikhub.io', 'api.tikhub.io', 'user.tikhub.io'}:
            raise StopRun('Price source must be an official TikHub HTTPS page.')
        try:
            age = datetime.now(timezone.utc).timestamp() - stamp(pricing['checked_at'])
        except (TypeError, ValueError):
            raise StopRun('pricing.checked_at must be an ISO timestamp with timezone.')
        if age < -300 or age > 7 * DAY:
            raise StopRun('Recheck the endpoint prices; the recorded quote is over 7 days old or in the future.')
        for op in operations:
            cost = money(pricing.get('usd_per_request', {}).get(op), op + ' unit price')
            if cost <= 0:
                raise StopRun('A positive, checked USD price is required for ' + op)

def within_search(v, c, as_of):
    ts = v.get('published_at')
    return isinstance(ts, int) and as_of - c['search_days'] * DAY <= ts <= as_of

def select_baseline(target, videos, c, as_of):
    eligible = {}
    for v in videos:
        ts = v.get('published_at')
        if v['id'] == target['id'] or v.get('author_id') != target.get('author_id'):
            continue
        if not isinstance(ts, int) or as_of - ts < c['min_age_days'] * DAY:
            continue
        if abs(ts - target['published_at']) <= c['comparison_window_days'] * DAY:
            eligible[v['id']] = v
    return sorted(eligible.values(), key=lambda v: (
        abs(v['published_at'] - target['published_at']), v['id']))[:c['baseline_count']]

def history_sufficient(target, baseline, history, c):
    if target['id'] not in history['videos']:
        return False
    if history.get('complete'):
        return True
    frontier = history.get('frontier')
    if frontier is None:
        return False
    if frontier <= target['published_at'] - c['comparison_window_days'] * DAY:
        return True
    if len(baseline) < c['baseline_count']:
        return False
    farthest = max(abs(v['published_at'] - target['published_at']) for v in baseline)
    return frontier <= target['published_at'] - farthest

def score(target, baseline, stats, c, *, listed=True, coverage=True, as_of=None):
    result = {'target': target, 'baseline': baseline, 'baseline_count': len(baseline),
              'play_count': None, 'median_plays': None, 'ratio': None,
              'status': 'pending', 'reason': '', 'limited_sample': len(baseline) < c['baseline_count']}
    def reject(status, reason):
        result.update(status=status, reason=reason)
        return result
    if target.get('followers') is None:
        return reject('pending', '粉丝数缺失，无法确认账号规模')
    if target['followers'] > c['max_followers']:
        return reject('excluded', '超出本次粉丝上限')
    if not listed:
        return reject('pending', '尚未在作者作品列表中确认目标')
    if as_of is not None and as_of - target['published_at'] < c['min_age_days'] * DAY:
        return reject('pending', '目标视频尚未达到最短观察天数')
    if not coverage:
        return reject('pending', '历史分页未覆盖所需时间邻域')
    if len(baseline) < c['min_baseline_count']:
        return reject('pending', '可用对照作品不足')
    required = [target] + baseline
    missing = [v['id'] for v in required if number(stats.get(v['id'], {}).get('play_count')) is None]
    if missing:
        result['missing_stats'] = missing
        return reject('pending', '目标或对照播放量缺失；未将缺失当作零')
    plays = stats[target['id']]['play_count']
    values = [stats[v['id']]['play_count'] for v in baseline]
    mid = statistics.median(values)
    result.update(play_count=plays, median_plays=mid,
                  baseline_plays={v['id']: stats[v['id']]['play_count'] for v in baseline})
    if mid == 0:
        return reject('pending', '对照中位数为零，不计算无限倍数')
    result['ratio'] = plays / mid
    if result['ratio'] < float(c['min_ratio']):
        return reject('below_threshold', '未达到设定的播放倍数')
    return reject('limited' if result['limited_sample'] else 'qualified',
                  '达到倍数，但对照少于期望数量' if result['limited_sample'] else '达到本次筛选标准')
