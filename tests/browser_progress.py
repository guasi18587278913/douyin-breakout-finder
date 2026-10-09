"""A verified result is usable while writing is still blocked; no provider calls."""
import argparse
import json
import tempfile
import threading
from pathlib import Path
from unittest.mock import patch
from playwright.sync_api import sync_playwright, expect
from test_analyst import offline_analyst, offline_data, LocalAnalyst
from wizard import make_server


def main():
    p=argparse.ArgumentParser();p.add_argument('--out',required=True);args=p.parse_args()
    out=Path(args.out);out.mkdir(parents=True,exist_ok=True);checks=[]
    release=threading.Event();entered=threading.Event()
    with tempfile.TemporaryDirectory() as temp, offline_analyst(), offline_data(with_body=False), sync_playwright() as play:
        server=make_server(Path(temp)/'data',assistant_mode='codex')
        threading.Thread(target=server.serve_forever,daemon=True).start()
        base='http://127.0.0.1:%s'%server.server_port
        original=LocalAnalyst.generate
        def generate(self,stage,packet,feedback):
            if stage=='content-analysis':
                entered.set()
                if not release.wait(25):raise RuntimeError('test timeout')
            return original(self,stage,packet,feedback)
        browser=play.chromium.launch();page=browser.new_page(viewport={'width':1440,'height':1000})
        errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
        try:
            with patch.object(LocalAnalyst,'generate',generate):
                page.goto(base+'/#'+server.session.token)
                page.locator('#description').fill('FDE');page.locator('#start').click();page.locator('#start').click()
                page.locator('#remember-key').uncheck();page.locator('#key').fill('SYNTHETIC-PROGRESS-KEY')
                page.locator('#save-key').click()
                expect(page.locator('#highlights .highlight')).not_to_have_count(0,timeout=12000)
                assert entered.wait(5)
                assert server.session.status=='running'
                assert not (server.session.summary or {}).get('report_complete')
                checks.append('qualified cards appear while the report is still being written')
                count=len(server.session.usable_results)
                expect(page.locator('#highlights .highlight')).to_have_count(count)
                assert all(r['status']=='qualified' for r in server.session.usable_results)
                expect(page.locator('#highlights a').first).to_have_attribute('href',__import__('re').compile(r'https://www.douyin.com/video/\d+'))
                checks.append('only verified current results appear, with usable original links')
                page.screenshot(path=str(out/'first-results-desktop.png'),full_page=True)
                page.set_viewport_size({'width':390,'height':844})
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth+1')
                page.screenshot(path=str(out/'first-results-mobile.png'),full_page=True)
                checks.append('first results are readable on mobile without overflow')
                release.set();expect(page).to_have_url(base+'/report/report.html',timeout=15000)
                checks.append('completion still opens the final report in the same tab')
                timing=json.loads((server.session.run_dir/'timings.json').read_text(encoding='utf-8'))
                assert 0 <= timing['first_usable_result_seconds'] < timing['elapsed_seconds']
                assert sum(row['model_generations'] for row in timing['stages'])>=3
                checks.append('first-result time, total time and model attempt counts are recorded')
                assert not errors;checks.append('no browser script errors')
        finally:
            release.set();server.session.cancelled.set()
            if server.session.worker:server.session.worker.join(3)
            browser.close();server.shutdown();server.server_close()
    result={'passed':len(checks),'failed':0,'checks':checks,'real_model_calls':0,'real_paid_queries':0}
    (out/'acceptance.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(result,ensure_ascii=False))


if __name__=='__main__':main()
