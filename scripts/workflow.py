"""Resumable search, semantic-review handoff and same-author comparisons."""
import copy
from collections import Counter
from parallel_requests import call_many

MAX_SEARCH_ROUNDS = 3
VERIFY_BATCH_SIZE = 3
from core import (APIError, BudgetStop, StopRun, DAY, atomic_json, digest, now,
                  read_json, score, select_baseline, stamp, within_search, history_sufficient, money)

class CachedClient:
    def __init__(self, store):
        self.store = store
    def call(self, op, params):
        key = digest({'op': op, 'params': params})
        record = read_json(self.store.root / 'cache' / (key + '.json'))
        if record is None:
            raise APIError('Offline cache miss; no network request was made.')
        return record['data']

def search_period(days):
    return str(1 if days <= 1 else 7 if days <= 7 else 180 if days <= 180 else 0)

def record_stop(store, phase, error):
    store.state['stops'].append({'at': now(), 'phase': phase, 'message': str(error)})
    store.save()

def search(store, client, keywords=None, reason=None, page_limit=None):
    c, s = store.config, store.state
    keywords = list(dict.fromkeys(keywords or c['keywords']))
    if page_limit == 1 and len(keywords) > 1:
        return search_batch(store, client, keywords, reason)
    if any(k not in c['keywords'] for k in keywords) and not reason:
        raise StopRun('Supply --reason when expanding beyond the original keywords.')
    for kw in keywords:
        if not isinstance(kw, str) or not kw.strip() or len(kw) > 200:
            raise StopRun('Invalid keyword')
        q = s['queries'].setdefault(kw, {'pages': 0, 'cursor': 0, 'search_id': '', 'backtrace': '',
                                         'complete': False, 'seen_cursors': [], 'ids': [],
                                         'new_to_run': [], 'reason': reason or 'initial seed'})
        cap=c['max_search_pages'] if page_limit is None else min(c['max_search_pages'],q['pages']+page_limit)
        while not q['complete'] and q['pages'] < cap:
            params = {'keyword': kw, 'cursor': q['cursor'], 'search_id': q['search_id'],
                      'backtrace': q['backtrace'], 'sort_type': '1',
                      'publish_time': search_period(c['search_days']),
                      'filter_duration': '0', 'content_type': '1'}
            try:
                result = client.call('search', params)
            except BudgetStop as e:
                record_stop(store, 'search', e)
                return
            except APIError as e:
                q['error'] = str(e)
                record_stop(store, 'search', e)
                break
            q.pop('error', None)
            q['last_new_ids'], q['last_page_ids'] = [], []
            for v in result['videos']:
                if not within_search(v, c, stamp(s['as_of'])):
                    continue
                vid = v['id']
                if vid not in q['last_page_ids']: q['last_page_ids'].append(vid)
                if vid not in q['ids']:
                    q['ids'].append(vid)
                if vid not in s['candidates']:
                    s['candidates'][vid] = dict(v, queries=[], review={'decision': 'pending', 'reason': ''})
                    q['new_to_run'].append(vid)
                    q['last_new_ids'].append(vid)
                existing = s['candidates'][vid]
                if kw not in existing['queries']:
                    existing['queries'].append(kw)
                # Keep the original review; refresh only non-null public fields.
                existing.update({k: val for k, val in v.items() if val is not None})
            q['pages'] += 1
            old_cursor = q['cursor']
            q['seen_cursors'].append(old_cursor)
            nxt = result.get('cursor')
            q['complete'] = not result['has_more']
            if result['has_more'] and (nxt is None or nxt == old_cursor or nxt in q['seen_cursors']):
                q['complete'] = True
                q['warning'] = 'Repeated or missing cursor; stopped pagination.'
            if not q['complete']:
                q['cursor'] = nxt
                q['search_id'] = result.get('search_id') or ''
                q['backtrace'] = result.get('backtrace') or ''
            store.save()
    write_inbox(store)

def search_batch(store, client, keywords, reason=None):
    c, s = store.config, store.state
    if any(k not in c['keywords'] for k in keywords) and not reason:
        raise StopRun('Supply --reason when expanding beyond the original keywords.')
    queue = []
    for kw in keywords:
        if not isinstance(kw, str) or not kw.strip() or len(kw) > 200:
            raise StopRun('Invalid keyword')
        q = s['queries'].setdefault(kw, {'pages': 0, 'cursor': 0, 'search_id': '', 'backtrace': '',
             'complete': False, 'seen_cursors': [], 'ids': [], 'new_to_run': [], 'reason': reason or 'initial seed'})
        if q['complete'] or q['pages'] >= c['max_search_pages']:
            continue
        params = {'keyword': kw, 'cursor': q['cursor'], 'search_id': q['search_id'], 'backtrace': q['backtrace'],
                  'sort_type': '1', 'publish_time': search_period(c['search_days']), 'filter_duration': '0', 'content_type': '1'}
        queue.append((kw, q, params))
    responses = call_many(client, [('search', params, {}) for _, _, params in queue])
    fatal = None
    for (kw, q, params), (result, error) in zip(queue, responses):
        if error:
            if isinstance(error, (APIError, BudgetStop)):
                q['error'] = str(error); record_stop(store, 'search', error)
            else:
                fatal = fatal or error
            continue
        q.pop('error', None)
        q['last_new_ids'], q['last_page_ids'] = [], []
        for v in result['videos']:
            if not within_search(v, c, stamp(s['as_of'])):
                continue
            vid = v['id']
            if vid not in q['last_page_ids']: q['last_page_ids'].append(vid)
            if vid not in q['ids']: q['ids'].append(vid)
            if vid not in s['candidates']:
                s['candidates'][vid] = dict(v, queries=[], review={'decision': 'pending', 'reason': ''})
                q['new_to_run'].append(vid)
                q['last_new_ids'].append(vid)
            existing = s['candidates'][vid]
            if kw not in existing['queries']: existing['queries'].append(kw)
            existing.update({k: value for k, value in v.items() if value is not None})
        q['pages'] += 1
        old = q['cursor']; q['seen_cursors'].append(old)
        nxt = result.get('cursor'); q['complete'] = not result['has_more']
        if result['has_more'] and (nxt is None or nxt == old or nxt in q['seen_cursors']):
            q['complete'] = True; q['warning'] = 'Repeated or missing cursor; stopped pagination.'
        if not q['complete']:
            q['cursor'] = nxt; q['search_id'] = result.get('search_id') or ''; q['backtrace'] = result.get('backtrace') or ''
        store.save()
    write_inbox(store)
    if fatal: raise fatal


def search_extensions(store, proposed, limit=2):
    """Prefer productive pages and one new angle, leaving room to verify discoveries."""
    c, state = store.config, store.state
    prices = c['pricing']['usd_per_request']
    remaining_requests = c['max_requests'] - len(store.reservations())
    remaining_usd = money(c['budget_usd']) - store.reserved_cost()
    stats_calls = (c['baseline_count'] + 2) // 2
    # Leave space for author pagination plus one full set of comparison statistics.
    reserve_requests = c['max_author_pages'] + stats_calls
    reserve_usd = money(prices['posts']) * c['max_author_pages'] + money(prices['stats']) * stats_calls
    as_of = stamp(state['as_of'])
    pages = []
    for keyword, query in state['queries'].items():
        if query['complete'] or query.get('error') or query['pages'] >= c['max_search_pages']:
            continue
        fresh = query.get('last_new_ids', query['new_to_run'])
        useful = [state['candidates'][vid] for vid in fresh
                  if state['candidates'][vid]['review']['decision'] == 'include'
                  and state['candidates'][vid].get('followers') is not None
                  and state['candidates'][vid]['followers'] <= c['max_followers']
                  and as_of - state['candidates'][vid]['published_at'] >= c['min_age_days'] * DAY]
        if useful:
            size = max(1, len(query.get('last_page_ids', query['ids'])))
            pages.append((len(useful) / size, len(useful), keyword))
    pages.sort(reverse=True)
    page_words = [entry[2] for entry in pages]
    new_words = list(dict.fromkeys(word for word in proposed if word not in state['queries']))
    # Keep a productive keyword, while giving a new same-topic angle room to compete.
    choices = page_words[:1] + new_words[:1] + page_words[1:] + new_words[1:]
    selected = []
    for word in choices:
        if len(selected) >= limit:
            break
        count = len(selected) + 1
        if remaining_requests < count + reserve_requests:
            break
        if remaining_usd < money(prices['search']) * count + reserve_usd:
            break
        selected.append(word)
    return selected


def write_inbox(store):
    candidates = sorted(store.state['candidates'].values(), key=lambda x: x.get('likes') or 0, reverse=True)
    atomic_json(store.root / 'candidates.json', candidates)
    template = {v['id']: {'decision': v['review']['decision'], 'reason': v['review'].get('reason', ''),
                          'priority': v['review'].get('priority', 1),
                          'method': v['review'].get('method', 'human-or-agent')} for v in candidates}
    atomic_json(store.root / 'review-inbox.json', template)
    atomic_json(store.root / 'keyword-quality.json', keyword_quality(store))

def apply_review(store, reviews):
    if not isinstance(reviews, dict):
        raise StopRun('Review file must map video IDs to review objects.')
    checked = {}
    for vid, r in reviews.items():
        if vid not in store.state['candidates']:
            raise StopRun('Unknown candidate in review: ' + str(vid))
        if not isinstance(r, dict) or r.get('decision') not in ('include', 'exclude', 'unsure', 'pending'):
            raise StopRun('decision must be include, exclude, unsure or pending')
        if r['decision'] != 'pending' and (not isinstance(r.get('reason'), str) or not r['reason'].strip()):
            raise StopRun('Every reviewed decision needs a concrete relevance reason.')
        priority = r.get('priority', 1)
        if isinstance(priority, bool) or not isinstance(priority, int) or not 1 <= priority <= 3:
            raise StopRun('priority must be 1, 2 or 3; 3 is highest.')
        method = r.get('method', 'human-or-agent')
        if method not in ('keyword-rule', 'human-or-agent'):
            raise StopRun('Unknown review method')
        checked[vid] = {'decision': r['decision'], 'reason': r.get('reason', '')[:2000], 'priority': priority, 'method': method}
    for vid, review in checked.items():
        store.state['candidates'][vid]['review'] = review
        if vid in store.state['results']:
            store.state['results'][vid]['target']['review']=review
        if vid in store.state['results'] and review['decision'] != 'include':
            store.state['results'][vid].update(status='excluded', reason='相关性复核未纳入')
    store.save()
    write_inbox(store)

def keyword_quality(store):
    qs, candidates = store.state['queries'], store.state['candidates']
    result = []
    for name, q in qs.items():
        ids = q['ids']
        reviewed = [candidates[i]['review']['decision'] for i in ids
                    if candidates[i]['review']['decision'] in ('include', 'exclude')]
        include = reviewed.count('include')
        unique = [i for i in ids if len(candidates[i]['queries']) == 1]
        verified = sum(store.state['results'].get(i, {}).get('status') in ('qualified', 'limited') for i in ids)
        result.append({'keyword': name, 'returned_unique': len(ids), 'reviewed': len(reviewed),
                       'relevant': include, 'relevance_rate': include / len(reviewed) if reviewed else None,
                       'first_discovered': len(q['new_to_run']), 'exclusive_to_keyword': len(unique),
                       'verified_outliers': verified, 'pages': q['pages'],
                       'warning': q.get('error') or q.get('warning'), 'reason': q['reason']})
    return result

def fetch_history(store, client, target, channel='normal'):
    if channel not in ('normal', 'lite'):
        raise StopRun('channel must be normal or lite')
    aid = target['author_id']
    key = aid + ':' + channel
    h = store.state['authors'].setdefault(key, {'author_id': aid, 'channel': channel, 'pages': 0,
                                               'cursor': 0, 'seen_cursors': [], 'complete': False,
                                               'frontier': None, 'videos': {}, 'warning': None})
    c, as_of = store.config, stamp(store.state['as_of'])
    while True:
        baseline = select_baseline(target, h['videos'].values(), c, as_of)
        if history_sufficient(target, baseline, h, c) or h['complete'] or h['pages'] >= c['max_author_pages']:
            return h, baseline
        old_cursor = h['cursor']
        params = {'sec_user_id': aid, 'max_cursor': old_cursor, 'count': 20, 'sort_type': 0, 'channel': channel}
        result = client.call('posts', params)
        for v in result['videos']:
            if v['author_id'] == aid:
                h['videos'][v['id']] = v
        dates = [v['published_at'] for v in result['videos']
                 if not v['is_top'] and isinstance(v['published_at'], int)]
        if dates:
            h['frontier'] = min(dates + ([h['frontier']] if h['frontier'] is not None else []))
        h['pages'] += 1
        h['seen_cursors'].append(old_cursor)
        h['complete'] = not result['has_more']
        nxt = result.get('cursor')
        if result['has_more'] and (nxt is None or nxt == old_cursor or nxt in h['seen_cursors']):
            h['warning'] = 'Repeated or missing cursor; history coverage is incomplete.'
            h['pages'] = c['max_author_pages']
        elif not h['complete']:
            h['cursor'] = nxt
        store.save()

def prefetch_histories(store, client, targets, channel):
    """Fetch independent authors together, retaining per-author cursor dependencies."""
    c, s = store.config, store.state
    as_of = stamp(s['as_of']); groups = {}; failures = {}
    for target in targets:
        aid = target.get('author_id')
        if not aid or as_of - target['published_at'] < c['min_age_days'] * DAY:
            continue
        groups.setdefault(aid, []).append(target)
        s['authors'].setdefault(aid + ':' + channel, {'author_id': aid, 'channel': channel, 'pages': 0,
            'cursor': 0, 'seen_cursors': [], 'complete': False, 'frontier': None, 'videos': {}, 'warning': None})
    while True:
        queue = []
        for aid, related in groups.items():
            h = s['authors'][aid + ':' + channel]
            if aid in failures or h['complete'] or h['pages'] >= c['max_author_pages']:
                continue
            needed = any(not history_sufficient(t, select_baseline(t, h['videos'].values(), c, as_of), h, c) for t in related)
            if needed:
                queue.append((aid, h, {'sec_user_id': aid, 'max_cursor': h['cursor'], 'count': 20, 'sort_type': 0, 'channel': channel}))
        if not queue: break
        responses = call_many(client, [('posts', params, {}) for _, _, params in queue])
        for (aid, h, params), (result, error) in zip(queue, responses):
            if error:
                if not isinstance(error, (APIError, BudgetStop)): raise error
                failures[aid] = error; continue
            for v in result['videos']:
                if v['author_id'] == aid: h['videos'][v['id']] = v
            dates = [v['published_at'] for v in result['videos'] if not v['is_top'] and isinstance(v['published_at'], int)]
            if dates: h['frontier'] = min(dates + ([h['frontier']] if h['frontier'] is not None else []))
            old = h['cursor']; h['pages'] += 1; h['seen_cursors'].append(old); h['complete'] = not result['has_more']
            nxt = result.get('cursor')
            if result['has_more'] and (nxt is None or nxt == old or nxt in h['seen_cursors']):
                h['warning'] = 'Repeated or missing cursor; history coverage is incomplete.'; h['pages'] = c['max_author_pages']
            elif not h['complete']: h['cursor'] = nxt
            store.save()
    return failures


def restore_stats_and_attempts(store):
    """Recover saved projections, and count per-video attempts across regrouped requests."""
    attempts = Counter()
    logged_keys = set()
    for e in store.events:
        if e.get('event') == 'request' and e.get('operation') == 'stats':
            logged_keys.add(e['key'])
            attempts.update(set(e['params']['aweme_ids'].split(',')))
    snapshots = []
    for path in (store.root / 'cache').glob('*.json'):
        record = read_json(path)
        if record.get('operation') == 'stats':
            snapshots.append((record.get('captured_at', ''), path.stem, record))
    for _, key, record in sorted(snapshots):
        requested = record['params']['aweme_ids'].split(',')
        if key not in logged_keys:
            attempts.update(set(requested))
        for vid in requested:
            store.state['stats'].setdefault(vid, {'play_count': None})
        for vid, data in record['data']['statistics'].items():
            if data.get('play_count') is not None:
                store.state['stats'][vid] = dict(data)
    for vid, count in store.state.get('stats_checked', {}).items():
        attempts[vid] = max(attempts[vid], count)
    return attempts

def get_stats(store, client, ids):
    attempts = restore_stats_and_attempts(store)
    retry = getattr(client, 'retry_failed', False)
    missing = sorted({i for i in ids if store.state['stats'].get(i, {}).get('play_count') is None
                      and (attempts[i] == 0 or (retry and attempts[i] < 2))})
    batches = [missing[start:start + 2] for start in range(0, len(missing), 2)]
    calls = [('stats', {'aweme_ids': ','.join(batch)}, {'refresh': True} if retry and any(attempts[i] for i in batch) else {}) for batch in batches]
    errors = []
    for batch, (result, error) in zip(batches, call_many(client, calls)):
        if error:
            errors.append(error); continue
        checked = store.state.setdefault('stats_checked', {})
        for vid in batch:
            checked[vid] = attempts[vid] + 1
            store.state['stats'].setdefault(vid, {'play_count': None})
        for vid, data in result['statistics'].items():
            store.state['stats'][vid] = dict(data)
        store.save()
    if errors:
        raise next((e for e in errors if not isinstance(e, (APIError, BudgetStop))), errors[0])


def verification_fingerprint(store, target, result):
    """Keep an immutable identity/statistics snapshot; result.target can alias candidates."""
    ids = [target['id']] + [v['id'] for v in result.get('baseline', [])]
    return digest({'target': {k: target.get(k) for k in ('id', 'author_id', 'followers', 'published_at', 'is_top')},
                   'statistics': {vid: store.state['stats'].get(vid) for vid in ids}})


def verification_batch_size(store):
    c = store.config
    remaining = c['max_requests'] - len(store.reservations())
    prices = c['pricing']['usd_per_request']
    stats_calls = (c['baseline_count'] + 2) // 2
    calls_per_target = c['max_author_pages'] + stats_calls
    try:
        cost = money(prices['posts']) * c['max_author_pages'] + money(prices['stats']) * stats_calls
        credit = money(c['budget_usd']) - store.reserved_cost()
        capacity = min(remaining // calls_per_target, int(credit / cost))
    except (StopRun, ArithmeticError):
        capacity = 1
    # A single target can still complete entirely from already saved data.
    return max(1, min(VERIFY_BATCH_SIZE, capacity))


def verify(store, client, channel='normal'):
    c, s = store.config, store.state
    restore_stats_and_attempts(store)
    all_reviewed = [v for v in s['candidates'].values() if v['review']['decision'] == 'include']
    if not all_reviewed:
        raise StopRun('No included candidates. Review relevance before querying author lists and view counts.')
    ranked = sorted(all_reviewed, key=lambda v: (v['review'].get('priority', 1), v.get('likes') or 0), reverse=True)
    selected = []
    def waiting(t, reason):
        return {'target': t, 'baseline': [], 'status': 'pending', 'reason': reason,
                'play_count': None, 'median_plays': None, 'ratio': None, 'limited_sample': True}
    for t in ranked:
        if t.get('followers') is None or t['followers'] > c['max_followers']:
            s['results'][t['id']] = score(t, [], s['stats'], c)
            continue
        if not t.get('author_id'):
            s['results'][t['id']] = score(t, [], s['stats'], c, listed=False)
            continue
        if stamp(s['as_of']) - t['published_at'] < c['min_age_days'] * DAY:
            s['results'][t['id']] = score(t, [], s['stats'], c, as_of=stamp(s['as_of']))
            continue
        previous = s['results'].get(t['id'], {})
        same_target = (previous.get('history_key') == t['author_id'] + ':' + channel
                       and previous.get('verification_fingerprint') == verification_fingerprint(store, t, previous))
        if previous.get('status') in ('qualified', 'limited', 'below_threshold') and same_target:
            previous['target'] = t
            continue
        if len(selected) < c['max_candidates']:
            selected.append(t)
            s['results'][t['id']] = waiting(t, '已纳入，等待统计核验')
        else:
            s['results'].setdefault(t['id'], waiting(t, '达到本轮候选核验数量上限，尚未处理'))
    store.save()
    history_failures = {}
    batch_end = 0
    for index, target in enumerate(selected):
        if index >= batch_end and getattr(client, 'parallel_safe', False):
            batch_end = index + verification_batch_size(store)
            batch = [t for t in selected[index:batch_end]
                     if t['author_id'] not in history_failures]
            history_failures.update(prefetch_histories(store, client, batch, channel))
        baseline = []
        if not target.get('author_id'):
            s['results'][target['id']] = score(target, [], s['stats'], c, listed=False)
            store.save()
            continue
        if stamp(s['as_of']) - target['published_at'] < c['min_age_days'] * DAY:
            s['results'][target['id']] = score(target, [], s['stats'], c, as_of=stamp(s['as_of']))
            store.save()
            continue
        try:
            if target['author_id'] in history_failures:
                available = s['authors'][target['author_id'] + ':' + channel]
                baseline = select_baseline(target, available['videos'].values(), c, stamp(s['as_of']))
                enough_history = history_sufficient(target, baseline, available, c) and len(baseline) >= c['min_baseline_count']
                if not enough_history:
                    raise history_failures[target['author_id']]
            history, baseline = fetch_history(store, client, target, channel)
            listed = target['id'] in history['videos']
            coverage = history_sufficient(target, baseline, history, c)
            if listed and coverage and len(baseline) >= c['min_baseline_count']:
                get_stats(store, client, [target['id']] + [v['id'] for v in baseline])
            s['results'][target['id']] = score(target, baseline, s['stats'], c, listed=listed,
                                             coverage=coverage, as_of=stamp(s['as_of']))
            s['results'][target['id']]['history_key'] = target['author_id'] + ':' + channel
            s['results'][target['id']]['verification_fingerprint'] = verification_fingerprint(store, target, s['results'][target['id']])
        except BudgetStop as e:
            s['results'][target['id']] = {'target': target, 'baseline': baseline, 'status': 'pending',
                                         'reason': '预算已用完，保留已取得数据', 'median_plays': None,
                                         'play_count': None, 'ratio': None, 'limited_sample': True}
            record_stop(store, 'verify', e)
            for queued in selected:
                r = s['results'][queued['id']]
                if r['status'] == 'pending' and r['reason'] == '已纳入，等待统计核验':
                    r['reason'] = '本轮预算已用完，尚未核验'
            break
        except APIError as e:
            s['results'][target['id']] = {'target': target, 'baseline': baseline, 'status': 'pending',
                                         'reason': str(e), 'median_plays': None, 'play_count': None,
                                         'ratio': None, 'limited_sample': True}
            record_stop(store, 'verify', e)
        store.save()
    store.save()
    atomic_json(store.root / 'keyword-quality.json', keyword_quality(store))

def comments(store, client, ids):
    if not ids:
        raise StopRun('Specify which selected video IDs need a comment sample.')
    for vid in ids:
        result = store.state['results'].get(vid)
        if not result or result['status'] not in ('qualified', 'limited'):
            raise StopRun('Comments are limited to selected results; first verify ' + vid)
    for vid in ids:
        try:
            response = client.call('comments', {'aweme_id': vid, 'cursor': 0, 'count': 20})
            store.state['comments'][vid] = {'items': response['comments'], 'sample_note':
                '接口默认返回的首批评论，最多20条；不是代表性民意样本，也不保证按热度排序。'}
            store.save()
        except BudgetStop as e:
            record_stop(store, 'comments', e)
            return
        except APIError as e:
            record_stop(store, 'comments', e)

def annotate(store, data):
    if not isinstance(data, dict):
        raise StopRun('Analysis must map selected video IDs to objects.')
    accepted = {}
    for vid, a in data.items():
        r = store.state['results'].get(vid)
        if not r or r['status'] not in ('qualified', 'limited') or not isinstance(a, dict):
            raise StopRun('Only selected results can receive analysis: ' + str(vid))
        if a.get('basis') not in ('title-description', 'transcript', 'video-inspected'):
            raise StopRun('Analysis basis must be title-description, transcript or video-inspected.')
        if not isinstance(a.get('evidence_note'), str) or not a['evidence_note'].strip():
            raise StopRun('Explain what was actually read or inspected in evidence_note.')
        accepted[vid] = {k: str(a.get(k, ''))[:6000] for k in (
            'basis', 'evidence_note', 'summary', 'opening', 'structure', 'useful_angle', 'comments_summary')}
    store.state['analyses'].update(accepted)
    store.save()
