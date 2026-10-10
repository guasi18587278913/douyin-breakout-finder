"""TikHub direct adapter. No private configuration discovery and no automatic retry."""
import json
import os
import threading
import time
from concurrent.futures import Future
import re
import urllib.error
import urllib.parse
import urllib.request
from core import APIError, BudgetStop, StopRun, atomic_json, digest, now, number, read_json

BASE = 'https://api.tikhub.io'
ENDPOINTS = {
    'search': ('POST', '/api/v1/douyin/search/fetch_video_search_v2'),
    'posts': ('GET', '/api/v1/douyin/app/v3/fetch_user_post_videos'),
    'stats': ('GET', '/api/v1/douyin/app/v3/fetch_video_statistics'),
    'stats_bulk': ('GET', '/api/v1/douyin/app/v3/fetch_multi_video_statistics'),
    'comments': ('GET', '/api/v1/douyin/app/v3/fetch_video_comments'),
}

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise APIError('TikHub redirected the request. Stopped without forwarding the API key.')

def video(a, context_author=None):
    if not isinstance(a, dict):
        return None
    vid = str(a.get('aweme_id', ''))
    if not re.fullmatch(r'\d{10,30}', vid):
        return None
    author = a.get('author') or {}
    if not isinstance(author, dict):
        return None
    sid = author.get('sec_uid')
    if not sid and context_author:
        sid = context_author
    if sid is not None and not re.fullmatch(r'[A-Za-z0-9_-]{8,200}', str(sid)):
        sid = None
    s = a.get('statistics') or {}
    v = a.get('video') or {}
    m = a.get('music') or {}
    dur = number(v.get('duration'))
    audio = None
    if (m.get('is_original_sound') is True and
            str(m.get('owner_id')) == str(author.get('uid')) and dur is not None and
            isinstance(m.get('duration'), (int, float)) and abs(m['duration'] - dur / 1000) < 3):
        audio = first_https((m.get('play_url') or {}).get('url_list'))
    return {
        'id': vid, 'title': str(a.get('desc') or '')[:6000],
        'author_id': str(sid) if sid else None,
        'author_uid': str(author.get('uid') or ''),
        'author_name': str(author.get('nickname') or '')[:200],
        'followers': number(author.get('follower_count')),
        'published_at': number(a.get('create_time')),
        'likes': number(s.get('digg_count')), 'collects': number(s.get('collect_count')),
        'comment_count': number(s.get('comment_count')),
        'is_top': a.get('is_top') in (1, True, '1'),
        'duration_seconds': dur / 1000 if dur is not None else None,
        'cover_url': first_https((v.get('cover') or {}).get('url_list')),
        'original_audio_url': audio,
    }

def first_https(values):
    if not isinstance(values, list):
        return None
    for value in values:
        if isinstance(value, str):
            u = urllib.parse.urlsplit(value)
            if u.scheme == 'https' and u.hostname and not u.username and not u.password:
                return value
    return None

def unwrap(payload):
    if not isinstance(payload, dict):
        raise APIError('TikHub response is not a JSON object.')
    # Accept recorded gateway envelopes only for offline replay/import tests.
    if 'provider' in payload and isinstance(payload.get('data'), dict) and 'code' in payload['data']:
        payload = payload['data']
    if payload.get('code') not in (0, 200, '0', '200'):
        raise APIError('TikHub returned an unsuccessful outer code: %s' % str(payload.get('code'))[:30])
    data = payload.get('data')
    if not isinstance(data, dict):
        raise APIError('TikHub returned no structured data.')
    if data.get('status_code', 0) not in (0, '0'):
        raise APIError('Upstream business error: status_code=%s' % str(data.get('status_code'))[:30])
    return data

def pagination_flag(data):
    if not isinstance(data, dict) or 'has_more' not in data:
        raise APIError('Pagination schema changed: has_more is missing; coverage is unknown.')
    value = data['has_more']
    if type(value) not in (bool, int, str) or value not in (0, 1, False, True, '0', '1'):
        raise APIError('Pagination schema changed: has_more is invalid; coverage is unknown.')
    return value in (1, True, '1')

def normalize(op, payload, params):
    data = unwrap(payload)
    if op == 'search':
        cards = data.get('business_data')
        if not isinstance(cards, list):
            raise APIError('Search response schema changed: business_data is missing.')
        items = {}
        for card in cards:
            body = card.get('data', {}) if isinstance(card, dict) else {}
            if isinstance(body, str):
                try:
                    body = json.loads(body)
                except ValueError:
                    continue
            a = video(body.get('aweme_info')) if isinstance(body, dict) else None
            if a:
                items[a['id']] = a
        cfg = data.get('business_config') or data
        more = pagination_flag(cfg)
        nxt = cfg.get('next_page') or {}
        cursor = number(nxt.get('cursor', data.get('cursor')))
        if more and cursor is None:
            raise APIError('Search pagination cursor is missing or invalid.')
        return {'videos': list(items.values()), 'has_more': more,
                'cursor': cursor,
                'search_id': nxt.get('search_id', data.get('search_id', '')),
                'backtrace': cfg.get('backtrace', data.get('backtrace', ''))}
    if op == 'posts':
        more = pagination_flag(data)
        if 'aweme_list' not in data:
            raise APIError('Post response schema changed: aweme_list is missing.')
        if data.get('sec_uid') and data['sec_uid'] != params['sec_user_id']:
            raise APIError('Post response belongs to a different author.')
        items = {}
        fallback = data.get('sec_uid') if data.get('sec_uid') == params['sec_user_id'] else None
        raw_items = data.get('aweme_list') or []
        if not isinstance(raw_items, list):
            raise APIError('aweme_list is not a list.')
        for a in raw_items:
            item = video(a, fallback)
            if item and item['author_id'] == params['sec_user_id']:
                items[item['id']] = item
        cursor = number(data.get('max_cursor'))
        if more and cursor is None:
            raise APIError('Author pagination cursor is missing or invalid.')
        return {'videos': list(items.values()), 'has_more': more, 'cursor': cursor}
    if op in ('stats', 'stats_bulk'):
        if not isinstance(data.get('statistics_list'), list):
            raise APIError('Statistics response schema changed: statistics_list is missing.')
        allowed = set(params['aweme_ids'].split(','))
        items = {}
        for row in data['statistics_list']:
            if not isinstance(row, dict):
                continue
            vid = str(row.get('aweme_id', ''))
            if vid in allowed:
                items[vid] = {'play_count': number(row.get('play_count')),
                              'likes': number(row.get('digg_count'))}
        return {'statistics': items}
    if op == 'comments':
        if 'comments' not in data:
            raise APIError('Comment response schema changed: comments is missing.')
        rows = data.get('comments') or []
        if not isinstance(rows, list):
            raise APIError('comments is not a list.')
        return {'comments': [{'id': str(c.get('cid') or ''), 'text': str(c.get('text') or '')[:6000],
                              'likes': number(c.get('digg_count'))} for c in rows if isinstance(c, dict)],
                'has_more': data.get('has_more') in (1, True, '1'), 'cursor': data.get('cursor')}
    raise APIError('Unknown operation')

class RateLimitError(APIError):
    pass


class TikHubClient:
    def __init__(self, store, allow_paid=False, retry_failed=False, transport=None, api_key=None, request_interval=0.5):
        if store.state.get('demo'):
            raise StopRun('Demo runs cannot use a network client.')
        if not allow_paid:
            raise StopRun('Paid calls require --allow-paid after the user agreed to this run budget.')
        self.store = store
        self.retry_failed = retry_failed
        self.key = (api_key if api_key is not None else os.environ.get('TIKHUB_API_KEY', '')).strip()
        if not self.key or any(c in self.key for c in '\r\n'):
            raise StopRun('Set TIKHUB_API_KEY in this process environment; do not paste it into the chat or config.')
        self.transport = transport or self._send
        self.parallel_safe = True
        self._gate = threading.RLock()
        self._flights = {}
        self._next_start = {}
        self._interval = max(0, float(request_interval))
        self._fatal = None
        self.cancel_event = threading.Event()

    def _send(self, op, params):
        method, path = ENDPOINTS[op]
        url = BASE + path
        body = None
        if method == 'GET':
            url += '?' + urllib.parse.urlencode(params)
        else:
            body = json.dumps(params).encode('utf-8')
        from transport import request
        status, raw = request(url, method=method, payload=params if method == 'POST' else None, api_key=self.key)
        if status == 401:
            raise StopRun('TikHub Key 无效或已过期，请在右上角重新连接。')
        if status == 402:
            raise StopRun('TikHub 余额不足，或该接口不支持免费额度。请到 TikHub 后台检查余额后再试。')
        if status == 429:
            raise RateLimitError('TikHub 暂时限制了请求速度，已保留当前结果，请稍后再试。')
        if status == 403:
            raise StopRun('TikHub 拒绝访问。请检查 Key 权限、账号状态与网络；程序没有自动重试。')
        if status != 200:
            raise APIError('TikHub 暂时无法完成查询（HTTP %s），已保留当前结果。' % status)
        try:
            return json.loads(raw)
        except (ValueError, UnicodeError):
            raise APIError('TikHub 返回的数据格式异常，请稍后再试。')

    def _wait_for_slot(self, op):
        while True:
            if self.cancel_event.is_set():
                raise StopRun('用户已结束会话，不再发送新的查询。')
            with self._gate:
                if self._fatal:
                    raise self._fatal
                remaining = self._next_start.get(op, 0) - time.monotonic()
                if remaining <= 0:
                    self._next_start[op] = time.monotonic() + self._interval
                    return
            self.cancel_event.wait(min(remaining, 0.2))

    def call(self, op, params, refresh=False):
        if op not in ENDPOINTS:
            raise StopRun('Unsupported operation')
        key = digest({'op': op, 'params': params})
        flight_key = (key, bool(refresh))
        with self._gate:
            future = self._flights.get(flight_key)
            owner = future is None
            if owner:
                future = Future()
                self._flights[flight_key] = future
        if not owner:
            with self._gate:
                self.store.cache_hits = getattr(self.store, 'cache_hits', 0) + 1
            return future.result()
        try:
            result = self._call_once(op, params, key, refresh)
            future.set_result(result)
            return result
        except BaseException as error:
            if isinstance(error, Exception) and not isinstance(error, (APIError, BudgetStop)):
                message = str(error).replace(self.key, '[REDACTED]') if isinstance(error, StopRun) else '本地保存没有完成，已停止后续新查询。'
                with self._gate:
                    self._fatal = StopRun(message)
            future.set_exception(error)
            raise
        finally:
            with self._gate:
                self._flights.pop(flight_key, None)

    def _call_once(self, op, params, key, refresh):
        path = self.store.root / 'cache' / (key + '.json')
        # Cache lookup, attempt checks and budget reservation are one critical section.
        with self._gate:
            if self._fatal:
                raise self._fatal
            if self.cancel_event.is_set():
                raise StopRun('用户已结束会话，不再发送新的查询。')
            cached = read_json(path)
            if cached is not None and not refresh:
                self.store.cache_hits = getattr(self.store, 'cache_hits', 0) + 1
                return cached['data']
            if refresh and not self.retry_failed:
                raise StopRun('Refreshing an incomplete response requires --retry-failed.')
            attempts = [e for e in self.store.reservations() if e['key'] == key]
            if attempts and (not self.retry_failed or len(attempts) >= 2):
                raise APIError('This request has an unresolved or failed prior attempt. Use --retry-failed once after checking it.')
            self.store.pricing_ready([op])
            self.store.reserve(op, key, retry=bool(attempts))
            self.store.event({'event': 'request', 'key': key, 'operation': op, 'params': params})
        try:
            self._wait_for_slot(op)
            payload = self.transport(op, params)
            projected = normalize(op, payload, params)
            safe = json.loads(json.dumps(projected, ensure_ascii=False).replace(self.key, '[REDACTED]'))
            record = {'operation': op, 'params': params, 'captured_at': now(), 'data': safe}
            with self._gate:
                atomic_json(path, record)
                self.store.event({'event': 'saved', 'key': key, 'operation': op,
                                  'cache': 'cache/' + key + '.json'})
            return safe
        except Exception as error:
            safe_error = str(error).replace(self.key, '[REDACTED]') if isinstance(error, StopRun) else type(error).__name__
            with self._gate:
                if isinstance(error, RateLimitError):
                    self._next_start[op] = max(self._next_start.get(op, 0), time.monotonic() + 2)
                elif not isinstance(error, APIError):
                    self._fatal = StopRun(safe_error if isinstance(error, StopRun) else '本地保存没有完成，已停止后续新查询。')
                try:
                    self.store.event({'event': 'failed', 'key': key, 'operation': op, 'error': safe_error})
                except OSError:
                    pass
            if isinstance(error, StopRun):
                raise
            raise StopRun('Could not safely store this response: %s. Reservation retained; inspect the journal before retrying.' % type(error).__name__)
