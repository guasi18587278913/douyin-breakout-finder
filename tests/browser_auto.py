"""Offline browser → subprocess analysis → report/retry acceptance."""
import argparse
import json
import tempfile
import threading
from pathlib import Path
from unittest.mock import patch
from playwright.sync_api import sync_playwright, expect
from test_analyst import offline_analyst, offline_data, LocalAnalyst, AssistanceNeeded
from wizard import make_server


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--out', required=True)
    args = parser.parse_args()
    out = Path(args.out); out.mkdir(parents=True, exist_ok=True)
    checks = []
    with tempfile.TemporaryDirectory() as temp, offline_analyst(), offline_data(), sync_playwright() as play:
        browser = play.chromium.launch()
        try:
            for provider in ('codex', 'claude', 'workbuddy'):
                server = make_server(Path(temp) / provider, assistant_mode=provider)
                threading.Thread(target=server.serve_forever, daemon=True).start()
                base = 'http://127.0.0.1:%s' % server.server_port
                page = browser.new_page(viewport={'width': 1440, 'height': 1100})
                errors = []; page.on('pageerror', lambda error: errors.append(str(error)))
                original = LocalAnalyst.generate
                fail_once = [True]
                def generate(self, stage, packet, feedback):
                    if stage == 'content-analysis' and fail_once[0]:
                        fail_once[0] = False
                        raise AssistanceNeeded('模拟分析连接中断，可在页面继续生成报告。')
                    return original(self, stage, packet, feedback)
                try:
                    with patch.object(LocalAnalyst, 'generate', generate):
                        page.goto(base + '/#' + server.session.token)
                        page.wait_for_load_state('networkidle')
                        page.get_by_role('textbox', name='你想找什么样的内容？').fill('FDE')
                        page.get_by_role('button', name='帮我找爆款', exact=True).click()
                        page.get_by_role('button', name='生成报告', exact=True).click()
                        page.locator('#key').fill('SYNTHETIC-BROWSER-KEY')
                        page.locator('#save-key').click()
                        expect(page.locator('#resume-analysis')).to_be_visible(timeout=15000)
                        expect(page.locator('#report-link-label')).to_have_text('查看阶段报告')
                        assert server.session.bridge is None
                        journal = (server.session.run_dir / 'requests.jsonl').read_bytes()
                        page.locator('#resume-analysis').click()
                        expect(page).to_have_url(base + '/report/report.html', timeout=15000)
                        expect(page.locator('.selection-report')).to_be_visible()
                        expect(page.locator('#new-research-link')).to_be_visible()
                        expect(page.locator('#resume-analysis')).to_be_hidden()
                        assert journal == (server.session.run_dir / 'requests.jsonl').read_bytes()
                        assert '回到启动' not in page.locator('body').inner_text()
                        assert not errors, errors
                        page.screenshot(path=str(out / (provider + '-complete.png')), full_page=True)
                        checks.append(provider + ': webpage starts analysis without host; offline retry preserves ledger and opens report directly')
                finally:
                    server.session.cancelled.set()
                    if server.session.worker:
                        server.session.worker.join(3)
                    page.close(); server.shutdown(); server.server_close()
        finally:
            browser.close()
    result = {'passed': len(checks), 'failed': 0, 'checks': checks, 'real_provider_calls': 0, 'real_model_calls': 0}
    (out / 'acceptance.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    main()
