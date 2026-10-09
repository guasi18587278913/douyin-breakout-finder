import contextlib
import copy
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from analyst import LocalAnalyst, choose_provider, child_environment, answer_schema, cli_prefix
from bridge import AssistanceNeeded
from core import StopRun, now, read_json
from demo import ReplayClient, run_demo
from research import add_evidence, analysis_packet
from wizard import Session


@contextlib.contextmanager
def offline_analyst(behavior='normal', audit='-'):
    """Use the real subprocess/parser lifecycle; replace only the external CLI."""
    original = LocalAnalyst.command
    def command(self, folder):
        built = original(self, folder)
        return [sys.executable, str(ROOT / 'tests/fake_analyst_cli.py'), self.name, behavior, str(audit)] + built[1:]
    def check(self):
        self.executable = self.name
    with patch.object(LocalAnalyst, 'available', return_value=True), patch.object(LocalAnalyst, 'check', check), patch.object(LocalAnalyst, 'command', command):
        yield


@contextlib.contextmanager
def offline_data(with_body=True):
    fixture = read_json(ROOT / 'assets/demo.json')
    fixture['queries']['FDE'] = list(dict.fromkeys(vid for rows in fixture['queries'].values() for vid in rows))
    quote = {'source_url': 'https://tikhub.io/pricing', 'checked_at': now(),
             'usd_per_request': {op: '0.001' for op in ['search', 'posts', 'stats', 'comments']}}
    def client(store, **kwargs):
        replay = ReplayClient.__new__(ReplayClient)
        replay.store, replay.fixture = store, fixture
        return replay
    def packet(store):
        if with_body:
            for vid, result in store.state['results'].items():
                if result['status'] not in ('qualified', 'limited'):
                    continue
                source = store.root / 'synthetic-source.txt'
                source.write_text('合成逐字稿：先说明重复录入的问题，再展示输入和输出核对步骤，最后说明人工确认。本段仅用于离线测试。', encoding='utf-8')
                add_evidence(store, vid, source, 'transcript', '离线测试构造，不是真实视频转录。')
                source.unlink()
        return analysis_packet(store)
    with patch('wizard.fetch_prices', return_value=quote), patch('wizard.TikHubClient', side_effect=client), patch('wizard.analysis_packet', side_effect=packet):
        yield


class AdapterContract(unittest.TestCase):
    def test_windows_npm_launcher_bypasses_command_interpreter(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            script = root / 'node_modules/vendor/cli.js'; script.parent.mkdir(parents=True); script.write_text('')
            shim = root / 'claude.cmd'; shim.write_text('@"%dp0%\\node.exe" "%dp0%\\node_modules\\vendor\\cli.js" %*')
            with patch('analyst.shutil.which', return_value='C:/node/node.exe'):
                self.assertEqual(cli_prefix(str(shim), windows=True), ['C:/node/node.exe', str(script)])
            shim.write_text('arbitrary unrecognized wrapper')
            with self.assertRaises(AssistanceNeeded):
                cli_prefix(str(shim), windows=True)

    def test_large_review_schema_fits_windows_native_command_limit(self):
        import subprocess
        packet = {'candidates': [{'id': str(1000000000000000000 + i)} for i in range(100)]}
        schema = answer_schema('semantic-review', packet)
        self.assertEqual(schema['$defs']['review']['properties']['priority']['enum'], [1, 2, 3])
        self.assertLess(len(subprocess.list2cmdline(['claude', '--json-schema', json.dumps(schema)])), 28000)

    def test_own_client_and_explicit_selection_do_not_fall_back(self):
        with patch.dict(os.environ, {'CLAUDECODE': '1'}, clear=True), patch('analyst.shutil.which', return_value=None):
            self.assertEqual(choose_provider(), 'claude')
            self.assertEqual(choose_provider('workbuddy'), 'codebuddy')
            self.assertEqual(choose_provider('codex'), 'codex')
            self.assertFalse(LocalAnalyst(threading.Event()).available())

    def test_installation_records_provider_and_multiple_clis_are_not_guessed(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {}, clear=True), patch('analyst.shutil.which', return_value='/fake/cli'):
            self.assertIsNone(choose_provider())
            (Path(temp) / '.installation.json').write_text('{"client":"workbuddy"}')
            self.assertEqual(choose_provider(root=temp), 'codebuddy')

    def test_subprocess_does_not_inherit_provider_keys_or_parent_session(self):
        with patch.dict(os.environ, {'TIKHUB_API_KEY': 'SECRET', 'OPENAI_API_KEY': 'SECRET', 'ANTHROPIC_API_KEY': 'SECRET', 'CLAUDECODE': '1', 'CODEX_THREAD_ID': 'parent'}):
            env = child_environment()
        for name in ['TIKHUB_API_KEY', 'OPENAI_API_KEY', 'ANTHROPIC_API_KEY', 'CLAUDECODE', 'CODEX_THREAD_ID']:
            self.assertNotIn(name, env)

    def test_command_disables_external_tools_and_custom_config(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            (folder / 'schema.json').write_text('{}')
            for name in ['codex', 'claude', 'codebuddy']:
                a = LocalAnalyst(threading.Event(), name); a.executable = name
                cmd = a.command(folder)
                self.assertFalse(any('dangerously' in arg for arg in cmd))
                if name == 'codex':
                    self.assertIn('--ignore-user-config', cmd)
                    self.assertIn('features.shell_tool=false', cmd)
                    self.assertIn('features.plugins=false', cmd)
                    self.assertIn('forced_login_method="chatgpt"', cmd)
                else:
                    self.assertEqual(cmd[cmd.index('--tools') + 1], '')
                    self.assertEqual(cmd[cmd.index('--setting-sources') + 1], '')
                    self.assertIn('--strict-mcp-config', cmd)

    def test_all_protocols_round_trip_without_a_chat_host(self):
        for name in ['codex', 'claude', 'codebuddy']:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temp, offline_analyst(), offline_data():
                s = Session(Path(temp) / 'runs', assistant_mode=name, bridge_timeout=5)
                s.mode, s.key = 'live', 'SYNTHETIC-NOT-REAL'
                s.start({'description': 'FDE', 'consent': True})
                s.worker.join(10)
                self.assertFalse(s.worker.is_alive())
                self.assertIsNone(s.bridge)
                self.assertEqual(s.status, 'done', s.message)
                self.assertTrue(s.summary['report_complete'])
                self.assertIsNone(s.key)
                self.assertIn('自动生成的离线合成报告', (s.run_dir / 'report.html').read_text())

    def test_title_only_analysis_stays_partial(self):
        with tempfile.TemporaryDirectory() as temp, offline_analyst(), offline_data(with_body=False):
            s = Session(temp, assistant_mode='codex', bridge_timeout=5)
            s.mode, s.key = 'live', 'SYNTHETIC'
            s.start({'description': 'FDE', 'consent': True}); s.worker.join(10)
            self.assertEqual(s.status, 'needs_analysis', s.message)
            self.assertFalse(s.summary['report_complete'])
            self.assertFalse(s.snapshot()['can_resume'])
            with self.assertRaises(StopRun):
                s.resume_analysis()

    def test_retry_restores_verification_from_paid_cache(self):
        from workflow import verify
        with tempfile.TemporaryDirectory() as temp, offline_analyst(), offline_data():
            s = Session(temp, assistant_mode='codex', bridge_timeout=5)
            s.mode, s.key = 'live', 'SYNTHETIC'
            def interrupted_verify(store, client):
                verify(store, client)
                store.state['results'] = {}; store.save()
                raise AssistanceNeeded('模拟统计缓存已保存、核验结果未提交时中断')
            with patch('wizard.verify', interrupted_verify):
                s.start({'description': 'FDE', 'consent': True}); s.worker.join(10)
            before = (s.run_dir / 'requests.jsonl').read_bytes()
            self.assertEqual(s.summary['qualified_count'], 0)
            with patch('wizard.TikHubClient', side_effect=AssertionError('no paid calls')):
                s.resume_analysis(); s.worker.join(10)
            self.assertEqual(s.status, 'done', s.message)
            self.assertGreater(s.summary['qualified_count'], 0)
            self.assertEqual((s.run_dir / 'requests.jsonl').read_bytes(), before)

    def test_retry_uses_existing_run_and_leaves_paid_ledger_unchanged(self):
        with tempfile.TemporaryDirectory() as temp, offline_analyst(), offline_data():
            s = Session(temp, assistant_mode='claude', bridge_timeout=5)
            s.mode, s.key = 'live', 'SYNTHETIC'
            original = LocalAnalyst.generate
            def fail_analysis(self, stage, packet, feedback):
                if stage == 'content-analysis':
                    raise AssistanceNeeded('模拟网络中断')
                return original(self, stage, packet, feedback)
            with patch.object(LocalAnalyst, 'generate', fail_analysis):
                s.start({'description': 'FDE', 'consent': True}); s.worker.join(10)
            self.assertEqual(s.status, 'needs_analysis')
            root = s.run_dir
            before = (root / 'requests.jsonl').read_bytes()
            with patch('wizard.TikHubClient', side_effect=AssertionError('retry cannot query')), patch('wizard.fetch_prices', side_effect=AssertionError('retry cannot fetch prices')):
                s.resume_analysis(); s.worker.join(10)
            self.assertEqual(s.status, 'done', s.message)
            self.assertEqual(s.run_dir, root)
            self.assertEqual((root / 'requests.jsonl').read_bytes(), before)

    def test_failed_handshake_never_constructs_paid_client(self):
        with tempfile.TemporaryDirectory() as temp, offline_analyst('fail'), offline_data(), patch('wizard.TikHubClient') as paid:
            s = Session(temp, assistant_mode='codex', bridge_timeout=5)
            s.mode, s.key = 'live', 'SYNTHETIC'
            s.start({'description': 'FDE', 'consent': True}); s.worker.join(10)
            self.assertEqual(s.status, 'needs_analysis')
            paid.assert_not_called()
            self.assertIsNone(s.summary)
            self.assertIsNone(s.key)

    def test_missing_adapter_stops_before_quote(self):
        with tempfile.TemporaryDirectory() as temp, patch('analyst.shutil.which', return_value=None), patch('wizard.fetch_prices') as prices:
            s = Session(temp, assistant_mode='workbuddy')
            s.mode, s.key = 'live', 'SYNTHETIC'
            with self.assertRaisesRegex(StopRun, 'CodeBuddy'):
                s.start({'description': 'FDE', 'consent': True})
            prices.assert_not_called()

    def test_timeout_cancellation_and_output_limit_stop_process(self):
        for behavior in ['slow', 'oversized']:
            with self.subTest(behavior=behavior), offline_analyst(behavior):
                a = LocalAnalyst(threading.Event(), 'codex', timeout=.5)
                start = time.monotonic()
                with self.assertRaises(AssistanceNeeded):
                    a.ask('prepare-research', {}, lambda x: x)
                self.assertLess(time.monotonic() - start, 4)
        event = threading.Event()
        with offline_analyst('slow'):
            a = LocalAnalyst(event, 'claude', timeout=10)
            timer = threading.Timer(.3, event.set); timer.start()
            accepted = []
            try:
                with self.assertRaises(AssistanceNeeded):
                    a.ask('prepare-research', {}, accepted.append)
            finally:
                timer.join()
            self.assertFalse(accepted)

    def test_only_three_invalid_responses_are_attempted(self):
        a = LocalAnalyst(threading.Event(), 'codex')
        with patch.object(a, 'generate', return_value={}) as generate:
            with self.assertRaises(AssistanceNeeded):
                a.ask('semantic-review', {}, lambda x: x['reviews'])
            self.assertEqual(generate.call_count, 3)

    def test_paths_media_urls_and_keys_are_not_sent_to_model(self):
        with tempfile.TemporaryDirectory() as temp, offline_analyst(audit=Path(temp) / 'audit.json'), patch.dict(os.environ, {'TIKHUB_API_KEY': 'SECRET'}):
            a = LocalAnalyst(threading.Event(), 'codex')
            a.ask('prepare-research', {'run_dir': '/private/data', 'file': '/private/evidence', 'original_audio_url': 'https://media/signed'}, lambda x: x)
            result = read_json(Path(temp) / 'audit.json')
            self.assertEqual(result['request']['packet'], {})
            self.assertNotIn('SECRET', json.dumps(result))


if __name__ == '__main__':
    unittest.main()
