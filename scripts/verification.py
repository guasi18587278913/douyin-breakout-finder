"""Read-only acceptance from saved inputs and actual outputs, independent of prose."""
import hashlib
from collections import Counter
from html.parser import HTMLParser
from pathlib import Path
import copy

from core import Store, StopRun, digest, history_sufficient, read_json, score, select_baseline, stamp, within_search


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
    for path in sorted((store.root / 'cache').glob('*.json')):
        record = read_json(path)
        if record.get('operation') != 'search':
            continue
        params = record['params']
        check('cache:' + path.stem, path.stem == digest({'op': 'search', 'params': params}),
              {'operation': 'search', 'key': path.stem})
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
    for vid, candidate in sorted(candidates.items()):
        review, result = candidate.get('review', {}), results.get(vid, {})
        if vid in pending:
            status, reason = 'needs_review', '未审核、判断不确定或审核依据已改变'
        elif review.get('decision') == 'exclude':
            status, reason = 'excluded', review.get('reason', '')
        else:
            status, reason = result.get('status', 'not_verified'), result.get('reason', '尚未进行播放核验')
        rows.append({'id': vid, 'status': status, 'reason': reason})
        check('candidate:' + vid, bool(reason) and status in
              ('needs_review', 'excluded', 'qualified', 'limited', 'below_threshold', 'pending', 'not_verified'),
              {'status': status, 'reason': reason})
    eligible = eligible_ids(store)
    for vid in sorted(results):
        if vid not in candidates or candidates[vid].get('review', {}).get('decision') != 'include':
            continue
        candidate, recorded = candidates[vid], results[vid]
        if recorded.get('status') not in ('qualified', 'limited', 'below_threshold', 'excluded'):
            continue
        history = state['authors'].get(recorded.get('history_key', str(candidate.get('author_id')) + ':normal'))
        early_exclusion = candidate.get('followers') is None or candidate['followers'] > config['max_followers']
        baseline = select_baseline(candidate, history['videos'].values(), config, stamp(state['as_of'])) if history and not early_exclusion else []
        calculated = score(candidate, baseline, state['stats'], config,
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
        raw = (store.root / 'report.html').read_bytes()
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
    return {'status': 'FAIL' if failed else 'PASS', 'passed': len(checks) - failed, 'failed': failed,
            'checks': checks, 'run_dir': str(store.root), 'report_sha256': report_hash,
            'inputs_sha256': digest({'config': config, 'state': state, 'events': store.events}),
            'coverage': {'scope': '已保存的标准化搜索响应；不证明供应商原始卡片或全平台覆盖',
                         'candidate_count': len(rows), 'outcomes': dict(outcomes), 'items': rows,
                         'search_exhaustive': False,
                         'query_stops': [{'keyword': key, 'pages': q.get('pages'),
                                          'pagination_guard': q.get('warning'), 'error': q.get('error'),
                                          'provider_end': bool(q.get('complete') and not q.get('warning'))}
                                         for key, q in state['queries'].items()],
                         'recorded_stops': state.get('stops', [])},
            'not_checked': ['供应商数据真实性', '正文事实与阅读质量', '未检索范围',
                            '实际页面显示与安装一致性（由交付检查器验证）']}
