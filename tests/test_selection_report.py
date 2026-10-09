import base64
import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from core import Store, StopRun, default_config
from demo import run_demo
from research import accept_research, analysis_packet, report_state
from selection_report import FORMAT, eligible_ids, instructions
from selection_render import opening
from report import render
from cover_cache import allowed_cover, cache_covers, valid_cached
from report_fixture import selection_answer

PNG = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=')


class SelectionReportContract(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store, _ = run_demo(Path(self.tmp.name) / 'run')
        self.answer = selection_answer(analysis_packet(self.store))
        for lesson in self.answer['lessons']:
            for case in lesson['cases']:
                case['evidence_ids'] = [case['video_id'] + ':title']

    def tearDown(self):
        self.tmp.cleanup()

    def test_title_only_report_completes_and_keeps_pending_data_separate(self):
        before = (self.store.root / 'requests.jsonl').read_bytes()
        story = accept_research(self.store, self.answer, expected_format=FORMAT)
        summary = render(self.store)
        self.assertEqual(story['kind'], 'selection-explainer')
        self.assertTrue(summary['report_complete'])
        self.assertEqual(summary['report_format'], FORMAT)
        self.assertEqual(summary['pending_count'], 1)
        self.assertEqual(before, (self.store.root / 'requests.jsonl').read_bytes())
        doc = (self.store.root / 'report.html').read_text(encoding='utf-8')
        self.assertIn('账号正常播放量', doc)
        self.assertNotIn('条线索未完成核验', doc)
        self.assertNotIn('待核验', doc)
        self.assertIn('依据标题与简介', doc)
        self.assertNotIn('正文证据待补</h3>', doc)

    def test_unreviewed_and_unqualified_cases_are_rejected(self):
        vid = self.answer['lessons'][0]['cases'][0]['video_id']
        for field, value in [('method', 'keyword-rule'), ('decision', 'unsure')]:
            with self.subTest(field=field):
                original = copy.deepcopy(self.store.state['candidates'][vid]['review'])
                self.store.state['candidates'][vid]['review'][field] = value
                with self.assertRaises(StopRun):
                    accept_research(self.store, self.answer)
                self.store.state['candidates'][vid]['review'] = original
        self.store.state['results'][vid]['status'] = 'pending'
        with self.assertRaises(StopRun):
            accept_research(self.store, self.answer)

    def test_unknown_evidence_duplicate_case_and_extra_stat_fields_rejected(self):
        variants = []
        a = copy.deepcopy(self.answer); a['lessons'][0]['cases'][0]['evidence_ids'] = ['unknown']; variants.append(a)
        a = copy.deepcopy(self.answer); a['lessons'].append(copy.deepcopy(a['lessons'][0])); variants.append(a)
        a = copy.deepcopy(self.answer); a['qualified_count'] = 99; variants.append(a)
        a = copy.deepcopy(self.answer); a['lessons'][0]['cases'][0]['play_count'] = 999; variants.append(a)
        for answer in variants:
            with self.subTest(answer=answer), self.assertRaises(StopRun):
                accept_research(self.store, answer)

    def test_unseen_spoken_content_and_model_stats_are_rejected(self):
        for paragraph in ('视频开头先展示了完整的操作结果。', '本条播放量达到了999万。', '这一条有 999 万次播放。', '作者粉丝不过三千，播放超过三十万。'):
            answer = copy.deepcopy(self.answer)
            answer['lessons'][0]['cases'][0]['paragraphs'][0] = paragraph
            with self.subTest(paragraph=paragraph), self.assertRaises(StopRun):
                accept_research(self.store, answer)

    def test_group_greeting_and_task_footer_are_rejected(self):
        for value in ('同学们，下午好！', '<!-- 通知摘要：任务完成。 -->', '对照中位数很高。', '视频开头先展示了完整的操作结果。'):
            answer = copy.deepcopy(self.answer); answer['intro'] = [value]
            with self.subTest(value=value), self.assertRaises(StopRun):
                accept_research(self.store, answer)

    def test_statistics_cannot_hide_in_closing_or_application(self):
        for field in ('closing', 'application'):
            answer = copy.deepcopy(self.answer)
            fabricated = '这条原视频获得了 999 万次播放，足以说明这个方法值得复制。'
            if field == 'closing': answer['closing']['paragraphs'] = [fabricated]
            else: answer['lessons'][0]['applications'][0]['idea'] = fabricated
            with self.subTest(field=field), self.assertRaises(StopRun):
                accept_research(self.store, answer)

    def test_format_cannot_be_downgraded_in_current_workflow(self):
        old = {key: self.store.state['research'][key] for key in ('headline', 'lead', 'findings', 'categories', 'cases', 'next_topics', 'limitations')}
        with self.assertRaises(StopRun):
            accept_research(self.store, old, expected_format=FORMAT)
        self.assertTrue(render(self.store)['report_complete'])  # Legacy report remains unchanged.

    def test_query_stat_and_event_changes_invalidate_completion(self):
        for field in ('queries', 'stats', 'events', 'stops'):
            with self.subTest(field=field):
                accept_research(self.store, self.answer)
                if field == 'queries':
                    saved = copy.deepcopy(self.store.state['queries']); self.store.state['queries']['later'] = {'pages': 0}
                elif field == 'stats':
                    saved = copy.deepcopy(self.store.state['stats']); self.store.state['stats']['later'] = {'play_count': 9}
                elif field == 'stops':
                    saved = copy.deepcopy(self.store.state['stops']); self.store.state['stops'].append({'phase':'search','message':'budget exhausted'})
                else:
                    saved = copy.deepcopy(self.store.events); self.store.events.append({'event': 'reserved', 'key': 'later'})
                self.assertEqual(report_state(self.store)['status'], 'awaiting_analysis')
                if field == 'events': self.store.events = saved
                else: self.store.state[field] = saved

    def test_standard_and_example_are_available_to_all_analysis_packets(self):
        packet = analysis_packet(self.store)
        self.assertEqual(packet['schema'], FORMAT)
        self.assertEqual(set(packet['featured_candidates']), eligible_ids(self.store))
        self.assertIn('工具看了很多', packet['instructions'])
        self.assertIn('report-example.json', instructions())

    def test_summary_is_not_published_before_report_write_succeeds(self):
        from unittest.mock import patch
        before=(self.store.root/'summary.json').read_bytes()
        accept_research(self.store,self.answer)
        with patch('report.Path.write_text',side_effect=OSError('disk full')), self.assertRaises(OSError):
            render(self.store)
        self.assertEqual((self.store.root/'summary.json').read_bytes(),before)

    def test_unreviewed_extra_is_not_exposed_as_a_qualified_result(self):
        vid=next(iter(eligible_ids(self.store)));extra='1111111111111111111'
        self.store.state['candidates'][extra]=copy.deepcopy(self.store.state['candidates'][vid])
        self.store.state['candidates'][extra].update(id=extra,title='UNREVIEWED-EXTRA')
        self.store.state['candidates'][extra]['review']['method']='keyword-rule'
        self.store.state['results'][extra]=copy.deepcopy(self.store.state['results'][vid])
        self.store.state['results'][extra]['target']=self.store.state['candidates'][extra]
        accept_research(self.store,self.answer);summary=render(self.store)
        self.assertEqual(summary['qualified_count'],1)
        self.assertNotIn('UNREVIEWED-EXTRA',(self.store.root/'report.html').read_text(encoding='utf-8'))
        self.assertNotIn('UNREVIEWED-EXTRA',(self.store.root/'results.csv').read_text(encoding='utf-8-sig'))

    def test_untrusted_original_metadata_is_escaped(self):
        vid = self.answer['lessons'][0]['cases'][0]['video_id']
        self.store.state['candidates'][vid]['title'] = '<script>alert(1)</script>'
        from workflow import apply_review
        apply_review(self.store, {vid:dict(self.store.state['candidates'][vid]['review'])})
        accept_research(self.store, self.answer); render(self.store)
        doc = (self.store.root / 'report.html').read_text(encoding='utf-8')
        self.assertNotIn('<script>alert(1)</script>', doc)
        self.assertIn('&lt;script&gt;alert(1)&lt;/script&gt;', doc)

    def test_original_covers_are_cached_without_keys_or_new_ledger_entries(self):
        accept_research(self.store, self.answer)
        self.store.state['demo'] = False
        for vid in self.store.state['research']['cases']:
            self.store.state['candidates'][vid]['cover_url'] = 'https://p3.douyinpic.com/original.png'
        journal = (self.store.root / 'requests.jsonl').read_bytes()
        fetcher = Mock(return_value=(200, PNG))
        receipt = cache_covers(self.store, fetcher)
        self.assertEqual(receipt['unavailable'], 0)
        for call in fetcher.call_args_list:
            self.assertNotIn('api_key', call.kwargs)
        self.assertEqual(journal, (self.store.root / 'requests.jsonl').read_bytes())
        self.assertTrue(all(valid_cached(x) for x in json.loads((self.store.root / 'covers.json').read_text(encoding='utf-8')).values()))
        fetcher.reset_mock(); cache_covers(self.store, fetcher); fetcher.assert_not_called()

    def test_cover_failure_preserves_readable_report(self):
        accept_research(self.store, self.answer)
        self.store.state['demo'] = False
        for vid in self.store.state['research']['cases']:
            self.store.state['candidates'][vid]['cover_url'] = 'https://p3.douyinpic.com/original.png'
        cache_covers(self.store, Mock(side_effect=TimeoutError()))
        accept_research(self.store, self.answer)
        self.assertTrue(render(self.store)['report_complete'])
        doc = (self.store.root / 'report.html').read_text(encoding='utf-8')
        self.assertIn('暂无原封面', doc)
        self.assertIn('看原视频', doc)

    def test_cover_urls_reject_non_cdn_private_targets_and_credentials(self):
        for url in ('http://p3.douyinpic.com/a', 'https://127.0.0.1/a', 'https://douyinpic.com.evil.test/a',
                    'https://user:password@p3.douyinpic.com/a', 'https://p3.douyinpic.com:444/a',
                    'https://api.tikhub.io/api/v1/test'):
            with self.subTest(url=url): self.assertFalse(allowed_cover(url))
        self.assertTrue(allowed_cover('https://p3-sign.douyinpic.com/original.webp?signature=example'))


class EmptySelectionContract(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store.create(Path(self.tmp.name) / 'run', default_config('咖啡', ['咖啡'], '0.5'))
        self.store.state['queries'] = {'咖啡': {'pages': 1, 'complete': False}}
        self.answer = selection_answer(analysis_packet(self.store))

    def tearDown(self): self.tmp.cleanup()

    def test_successful_empty_sample_can_finish_without_exhausting_all_pages(self):
        self.assertEqual(accept_research(self.store, self.answer)['status'], 'complete')

    def test_failed_query_stays_partial(self):
        self.store.state['queries']['咖啡']['error'] = 'connection failed'
        self.assertEqual(accept_research(self.store, self.answer)['status'], 'partial')

    def test_recovered_request_does_not_keep_historical_stop_as_blocker(self):
        self.store.state['stops'] = [{'phase': 'search', 'message': 'old error', 'at':'2026-10-09T06:00:00+00:00'}]
        self.store.events = [{'event': 'reserved', 'key': 'same-query','operation':'search'}, {'event': 'failed', 'key': 'same-query'}, {'event': 'saved', 'key': 'same-query','at':'2026-10-09T06:01:00+00:00'}]
        self.assertEqual(accept_research(self.store, self.answer)['status'], 'complete')
        self.store.events.append({'event': 'request', 'key': 'unfinished'})
        self.assertEqual(accept_research(self.store, self.answer)['status'], 'partial')

    def test_unresolved_budget_stop_is_not_a_complete_empty_report(self):
        self.store.state['stops'] = [{'phase':'search','message':'USD budget would be exceeded.','at':'2026-10-09T06:02:00+00:00'}]
        self.store.events = [{'event':'reserved','key':'first-page','operation':'search'}, {'event':'saved','key':'first-page','at':'2026-10-09T06:01:00+00:00'}]
        self.assertEqual(accept_research(self.store, self.answer)['status'], 'partial')

    def test_unverified_include_without_a_result_stays_partial(self):
        self.store.state['candidates']['1111111111111111111'] = {'id': '1111111111111111111', 'title': '咖啡教程',
            'review': {'decision': 'include', 'method': 'human-or-agent', 'reason': '相关'}}
        self.assertEqual(accept_research(self.store, self.answer)['status'], 'partial')


if __name__ == '__main__': unittest.main()
