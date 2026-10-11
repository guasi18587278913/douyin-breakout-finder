"""Read-only acceptance from saved inputs and actual outputs, independent of prose."""
import hashlib
from collections import Counter
from html.parser import HTMLParser
from pathlib import Path
import copy

from core import APIError, Store, StopRun, digest, history_sufficient, read_json, score, select_baseline, stamp, within_search, money


def raw_facts(store, records):
    """Rebuild numeric inputs in memory. Never save or fetch on an audit read."""
    from workflow import CachedClient, fetch_history, restore_stats_and_attempts
    facts = copy.copy(store)
    facts.state = copy.deepcopy(store.state)
    facts.state.update(authors={}, stats={}, stats_checked={})
    facts.save = lambda: None
    restore_stats_and_attempts(facts)
    for _, record in records:
        if record['operation'] != 'search': continue
        for video in record['data']['videos']:
            if video['id'] in facts.state['candidates']:
                facts.state['candidates'][video['id']].update({k:v for k,v in video.items() if v is not None})
    client = CachedClient(facts)
    channels = {(r['params'].get('sec_user_id'),r['params'].get('channel','normal'))
                for _,r in records if r['operation']=='posts'}
    for candidate in facts.state['candidates'].values():
        if not candidate.get('author_id'): continue
        for channel in ('normal','lite'):
            if (candidate['author_id'],channel) not in channels:continue
            try: fetch_history(facts, client, candidate,channel)
            except APIError: pass
    return facts


def execution_evidence(store, records, rows):
    """Recalculate frontiers and affordable follow-up space; do not trust stop text."""
    from workflow import MAX_SEARCH_ROUNDS
    config = store.config
    searches = {}
    for key, record in records:
        if record['operation'] == 'search':
            searches.setdefault(record['params']['keyword'], []).append((key, record))
    queries = []
    for keyword, pages in searches.items():
        # Follow provider cursor links, not a stored 'complete' flag or page count.
        chain, cursor, seen = [], 0, set()
        while cursor not in seen:
            matches = [(k,r) for k,r in pages if r['params']['cursor'] == cursor]
            if not matches: break
            key, record = matches[-1]; chain.append(key); seen.add(cursor)
            data = record['data']
            if not data['has_more']: break
            cursor = data.get('cursor')
        last = next((r for k,r in pages if chain and k == chain[-1]), None)
        end = bool(last and not last['data']['has_more'])
        guard = bool(last and last['data']['has_more'] and
                     (last['data'].get('cursor') is None or last['data'].get('cursor') in seen))
        queries.append({'keyword': keyword, 'pages': len(chain), 'request_keys': chain,
                        'provider_end': end, 'cursor_guard': guard,
                        'can_paginate': bool(last and not end and not guard),
                        'page_limit_reached': len(chain) >= config['max_search_pages']})
    cache_keys = {key for key,_ in records}
    requests = [e for e in store.events if e.get('event') == 'request']
    reserved = store.reservations()
    requested_keys = {e['key'] for e in requests}
    last_saved = {e['key']:i for i,e in enumerate(store.events) if e.get('event')=='saved'}
    last_failed = {e['key']:i for i,e in enumerate(store.events) if e.get('event')=='failed'}
    missing = [e for e in reserved if e['key'] not in cache_keys
               or (not store.state.get('demo') and e['key'] not in requested_keys)
               or last_failed.get(e['key'],-1)>last_saved.get(e['key'],-1)]
    failed_search = sorted({e['key'] for e in missing if e['operation'] == 'search'})
    remaining = config['max_requests'] - len(reserved)
    credit = money(config['budget_usd']) - store.reserved_cost()
    prices = config['pricing']['usd_per_request']
    stats_calls = (config['baseline_count'] + 2) // 2
    reserve = config['max_author_pages'] + stats_calls
    reserve_usd = money(prices['posts']) * config['max_author_pages'] + money(prices['stats']) * stats_calls
    batches = store.state.get('search_batches', [])
    search_keys = [e['key'] for e in reserved if e['operation'] == 'search']
    flattened = [key for batch in batches for key in batch]
    source_records = dict(records)
    first_words = {source_records[key]['params']['keyword'] for key in (batches[0] if batches else [])
                   if key in source_records and source_records[key]['operation']=='search'}
    bounded_rounds = (store.config.get('search_policy') == 'bounded-rounds-v1'
                      and not failed_search
                      and first_words == set(config['keywords'])
                      and len(flattened) == len(set(flattened))
                      and len(batches) == MAX_SEARCH_ROUNDS
                      and all(0 < len(batch) <= (3 if i == 0 else 2) for i,batch in enumerate(batches))
                      and flattened == search_keys)
    all_seeds = set(config['keywords']) <= {q['keyword'] for q in queries}
    page_bounds = bool(queries) and all(q['provider_end'] or q['page_limit_reached'] for q in queries)
    decisions = store.state.get('search_decisions', [])
    last_decision = decisions[-1] if decisions else {}
    # Empty proposals are an actual model decision, not an inferred stop. A
    # bounded exhausted keyword pool is sufficient only with this bound input.
    no_extension = (page_bounds and last_decision.get('proposed_keywords') == []
                    and last_decision.get('selected_keywords') == []
                    and last_decision.get('request_count') == len(reserved)
                    and last_decision.get('candidate_review_sha256') ==
                    digest({vid:v.get('review') for vid,v in store.state['candidates'].items()}))
    reason, legal = 'unproven_stop', False
    if failed_search: reason = 'search_failed'
    elif remaining <= 0: reason, legal = 'request_limit', True
    elif credit < money(prices['search']): reason, legal = 'cost_limit', True
    elif all_seeds and (remaining < reserve + 1 or credit < reserve_usd + money(prices['search'])):
        reason, legal = 'verification_reserve', True
    elif all_seeds and bounded_rounds: reason, legal = 'search_round_limit', True
    elif all_seeds and no_extension: reason, legal = 'known_keyword_scope_and_no_proposed_extension', True
    elif all_seeds and page_bounds: reason = 'known_keywords_exhausted_further_strategy_unproven'
    unresolved = [r['id'] for r in rows if r['status'] in ('needs_review','pending','not_verified','limited','missing')]
    return {'legal_stop': legal, 'stop_reason': reason,
            'stop_kind':'request_fault' if failed_search else 'policy_limit' if legal else 'evidence_missing',
            'stopping_allowed': bool(legal or failed_search), 'queries': queries,
            'unattempted_seed_keywords': sorted(set(config['keywords']) - {q['keyword'] for q in queries}),
            'unresolved_ids': unresolved, 'all_candidates_resolved': not unresolved,
            'failed_search_request_keys': failed_search,
            'unavailable_request_keys': [e['key'] for e in missing],
            'remaining_requests': remaining, 'remaining_usd': str(credit),
            'followup_reserve_requests': reserve, 'followup_reserve_usd': str(reserve_usd),
            'search_rounds_proven': len(batches) if bounded_rounds else None,
            'last_search_decision': last_decision,
            'scope_exhaustive': False}


class ReportHTML(HTMLParser):
    def __init__(self):
        super().__init__()
        self.cards = []
        self.links = []
        self.selection = False
        self.comparisons = {}
        self.current_card = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        classes = attrs.get('class', '').split()
        if 'selection-report' in classes:
            self.selection = True
        if 'selection-card' in classes:
            self.current_card = attrs.get('id', '').removeprefix('case-')
            self.cards.append(self.current_card)
        if 'selection-comparison' in classes and self.current_card:
            self.comparisons[self.current_card] = attrs.get('aria-label', '')
        if tag == 'a':
            self.links.append(attrs.get('href', ''))


def audit_run(root, require_report=True):
    """Does not save state, fetch data, or accept a recorded PASS as evidence."""
    store = Store(root)
    state, config = store.state, store.config
    candidates, results = state['candidates'], state['results']
    checks = []

    def check(name, passed, observed):
        checks.append({'id': name, 'status': 'PASS' if passed else 'FAIL', 'observed': observed})

    source_ids, filtered_ids = set(), set()
    records = [(p.stem, read_json(p)) for p in (store.root / 'cache').glob('*.json')]
    records.sort(key=lambda pair:(pair[1].get('captured_at',''),pair[0]))
    request_keys = {e['key'] for e in store.events if e.get('event') == 'request'}
    for event in store.events:
        if event.get('event')=='request':
            check('request:' + event['key'], event['key']==digest({'op':event['operation'],'params':event['params']}),
                  {'operation':event['operation'],'key':event['key']})
    for key, record in records:
        check('cache:' + key, key == digest({'op':record['operation'],'params':record['params']})
              and (state.get('demo') or key in request_keys), {'operation':record['operation'],'key':key})
        if record.get('operation') != 'search':
            continue
        params = record['params']
        for video in record['data']['videos']:
            (source_ids if within_search(video, config, stamp(state['as_of'])) else filtered_ids).add(video['id'])
    queried_ids = {vid for query in state['queries'].values() for vid in query.get('ids', [])}
    history_ids=set()
    if config.get('discover_author_history'):
        from history_discovery import from_cache
        history=from_cache(store);history_ids=set(history)
        fields=('id','title','author_id','followers','author_name','published_at','discovery')
        check('history-provenance',all(vid in candidates and all(candidates[vid].get(k)==v.get(k) for k in fields) for vid,v in history.items()),
              {'history_candidate_count':len(history),'source':'同作者原始作品页与搜索取得的账号快照'})
    all_source_ids=source_ids|history_ids
    check('candidate-accounting', all_source_ids == set(candidates) and source_ids == queried_ids,
          {'saved_in_scope': len(source_ids), 'candidates': len(candidates), 'query_ids': len(queried_ids),
           'missing_candidate_ids': sorted(all_source_ids - set(candidates)),
           'unbacked_candidate_ids': sorted(set(candidates) - all_source_ids),
           'query_mismatch_ids': sorted(queried_ids ^ source_ids),
           'outside_date_window_ids': sorted(filtered_ids - source_ids)})
    check('result-identities', set(results) <= set(candidates)
          and all(r.get('target', {}).get('id') == vid for vid, r in results.items()),
          {'orphan_result_ids': sorted(set(results) - set(candidates))})
    from research import pending_reviews, report_state
    from selection_report import FORMAT, eligible_ids, delivery_target
    pending = pending_reviews(store)
    rows = []
    facts = raw_facts(store, records)
    for vid, candidate in sorted(candidates.items()):
        identity_fields = ('id','author_id','followers','published_at','title')
        raw_candidate = facts.state['candidates'][vid]
        check('candidate-source:' + vid, all(candidate.get(k) == raw_candidate.get(k) for k in identity_fields),
              {'fields':identity_fields})
        review, result = candidate.get('review', {}), results.get(vid, {})
        if vid in pending:
            status, reason = 'needs_review', '未审核、判断不确定或审核依据已改变'
        elif review.get('decision') == 'exclude':
            status, reason = 'excluded', review.get('reason', '')
        else:
            status, reason = result.get('status', 'not_verified'), result.get('reason', '尚未进行播放核验')
        rows.append({'id': vid, 'status': status, 'reason': reason,
                     'relevance_decision':review.get('decision'), 'relevance_reason':review.get('reason'),
                     'numeric_status':result.get('status'),
                     'exclusion_kind': 'objective' if candidate.get('followers') is not None and candidate['followers'] > config['max_followers'] else 'relevance' if review.get('decision') == 'exclude' else None})
        check('candidate:' + vid, bool(reason) and status in
              ('needs_review', 'excluded', 'qualified', 'limited', 'below_threshold', 'pending', 'not_verified'),
              {'status': status, 'reason': reason})
    rows.extend({'id':vid,'status':'missing','reason':'原始缓存中的候选未进入运行状态',
                 'relevance_decision':None,'relevance_reason':None,'numeric_status':None,'exclusion_kind':None}
                for vid in sorted(all_source_ids-set(candidates)))
    eligible = eligible_ids(store)
    for vid in sorted(results):
        if vid not in candidates or candidates[vid].get('review', {}).get('decision') != 'include':
            continue
        candidate, recorded = candidates[vid], results[vid]
        if recorded.get('status') not in ('qualified', 'limited', 'below_threshold', 'excluded'):
            continue
        raw_candidate = facts.state['candidates'][vid]
        candidate = raw_candidate
        history = facts.state['authors'].get(recorded.get('history_key', str(candidate.get('author_id')) + ':normal'))
        early_exclusion = candidate.get('followers') is None or candidate['followers'] > config['max_followers']
        baseline = select_baseline(candidate, history['videos'].values(), config, stamp(state['as_of'])) if history and not early_exclusion else []
        calculated = score(candidate, baseline, facts.state['stats'], config,
                           listed=bool(history and vid in history['videos']),
                           coverage=bool(history and history_sufficient(candidate, baseline, history, config)),
                           as_of=stamp(state['as_of']))
        fields = ('status', 'play_count', 'median_plays', 'ratio')
        check('statistics:' + vid, all(recorded.get(k) == calculated.get(k) for k in fields)
              and [v['id'] for v in recorded.get('baseline', [])] == [v['id'] for v in baseline],
              {key: calculated.get(key) for key in fields})
    reservations = store.reservations()
    from core import money
    check('budget', len(reservations) <= config['max_requests'] and store.reserved_cost() <= money(config['budget_usd']),
          {'requests': len(reservations), 'max_requests': config['max_requests'],
           'reserved_usd': str(store.reserved_cost()), 'limit_usd': config['budget_usd']})
    data_passed = all(c['status']=='PASS' for c in checks)
    report_hash = None
    if require_report:
        summary = read_json(store.root / 'summary.json', {})
        readiness = report_state(store)
        story = readiness.get('story') or {}
        valid_contract = False
        if story.get('format') == FORMAT:
            answer = {key:copy.deepcopy(story.get(key)) for key in
                      ('format', 'topic_label', 'intro', 'lessons', 'closing', 'limitations')}
            answer['title'] = story.get('headline')
            if isinstance(answer['lessons'], list):
                for lesson in answer['lessons']:
                    for case in lesson.get('cases', []):case.pop('basis', None)
            from selection_report import validate
            try:
                validated = validate(store, answer)
                valid_contract = validated['status'] == 'complete' and validated['cases'] == story.get('cases')
            except (StopRun, ValueError, TypeError, KeyError):
                pass
        if config.get('content_review_required'):
            from webapp.content_quality import verify_saved
            check('content-value-review',verify_saved(store),{'scope':'独立模型逐段语义审核，正文与证据摘要绑定；不代表人的阅读体验验收'})
        check('report-contract', valid_contract, {'revalidated_from_saved_answer': valid_contract})
        check('report-ready', readiness['status'] == 'complete' and summary.get('report_complete') is True
              and summary.get('report_format') == FORMAT,
              {'computed_state': readiness['status'], 'format': summary.get('report_format')})
        path = store.root / 'report.html'
        raw = path.read_bytes() if path.is_file() else b''
        report_hash = hashlib.sha256(raw).hexdigest()
        from report import render
        # Reconstruct every visible field and prose paragraph from the saved inputs.
        # This checks delivery integrity, not the literary quality of those inputs.
        expected_html = render(store, write=False).encode('utf-8')
        check('report-content', raw == expected_html,
              {'saved_sha256': report_hash, 'expected_sha256': hashlib.sha256(expected_html).hexdigest()})
        html = ReportHTML(); html.feed(raw.decode('utf-8'))
        check('report-cards', html.selection and len(html.cards) == len(eligible) and set(html.cards) == eligible,
              {'expected_ids': sorted(eligible), 'actual_ids': html.cards})
        def formatted(value):
            return format(value, ',.1f').rstrip('0').rstrip('.') if isinstance(value, float) else format(value, ',')
        check('rendered-statistics', all(html.comparisons.get(vid) == '这条作品播放量%s次，账号正常播放量%s次。' %
              (formatted(results[vid]['play_count']), formatted(results[vid]['median_plays'])) for vid in eligible),
              {'comparison_ids': sorted(html.comparisons)})
        if not state.get('demo'):
            check('original-video-links', all('https://www.douyin.com/video/' + vid in html.links for vid in eligible),
                  {'required_links': len(eligible)})
        expected = {'candidate_count': len(candidates), 'qualified_count': len(eligible),
                    'quantity_target_min':20,'quantity_target_met':len(eligible)>=20,
                    'delivery_target_count':delivery_target(store),'delivery_target_met':len(eligible)>=delivery_target(store),
                    'featured_count': len((readiness.get('story') or {}).get('cases', {})),
                    'request_attempts': len(reservations),
                    'pending_count': sum(r.get('status') == 'pending' for r in results.values())}
        check('summary-counts', all(summary.get(key) == value for key, value in expected.items()), expected)
    outcomes = Counter(row['status'] for row in rows)
    failed = sum(item['status'] == 'FAIL' for item in checks)
    from completion import target_policy
    policy = target_policy(store)
    execution = execution_evidence(store, records, rows)
    if not data_passed: execution['legal_stop'] = False
    path = store.root/'report.html'
    available = path.is_file() and b'<main' in path.read_bytes() and b'</html>' in path.read_bytes()
    ready = bool(require_report and not failed)
    numeric = {}
    for vid, candidate in facts.state['candidates'].items():
        aid=str(candidate.get('author_id'))
        history_key=state['results'].get(vid,{}).get('history_key',aid+':normal')
        history = (facts.state['authors'].get(history_key) or facts.state['authors'].get(aid+':normal')
                   or facts.state['authors'].get(aid+':lite'))
        baseline = select_baseline(candidate, history['videos'].values(), config, stamp(state['as_of'])) if history else []
        numeric[vid] = score(candidate, baseline, facts.state['stats'], config,
                            listed=bool(history and vid in history['videos']),
                            coverage=bool(history and history_sufficient(candidate, baseline, history, config)),
                            as_of=stamp(state['as_of']))['status']
    quantity = dict(policy, qualified_count=len(eligible), verified=data_passed,
                    met=data_passed and len(eligible)>=policy['effective_min'],
                    default_target_met=data_passed and len(eligible)>=20)
    completion = {'report':{'available':available,'ready':ready,'checked':require_report},
                  'retrieval':execution, 'quantity':quantity,
                  'cached_recovery':{'numerically_qualified_ids':sorted(vid for vid,status in numeric.items() if status=='qualified'),
                                     'data_incomplete':any(status=='pending' for status in numeric.values())},
                  'overall_goal_met': bool(not state.get('demo') and ready and execution['legal_stop']
                       and execution['all_candidates_resolved'] and quantity['met'])}
    return {'status': 'FAIL' if failed else 'PASS', 'scope':'saved_evidence_integrity',
            'completion':completion, 'passed': len(checks) - failed, 'failed': failed,
            'checks': checks, 'run_dir': str(store.root), 'report_sha256': report_hash,
            'inputs_sha256': digest({'config': config, 'state': state, 'events': store.events}),
            'coverage': {'scope': '已保存的标准化搜索响应；不证明供应商原始卡片或全平台覆盖',
                         'candidate_count': len(rows), 'outcomes': dict(outcomes), 'items': rows,
                         'search_exhaustive': False,
                         'query_stops': execution['queries'],
                         'recorded_stops': state.get('stops', [])},
            'not_checked': ['供应商数据真实性', '相关性判断是否符合用户原意（理由和输入绑定不代表语义正确）',
                            '正文事实与阅读质量', '未检索范围',
                            '实际页面显示与安装一致性（由交付检查器验证）']}
