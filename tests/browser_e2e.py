"""Browser acceptance against the installed public bundle, with no private API keys."""
import argparse
import json
import os
import platform
import sys
import threading
from pathlib import Path
from playwright.sync_api import sync_playwright, expect

p=argparse.ArgumentParser();p.add_argument('--bundle',required=True);p.add_argument('--out',required=True)
a=p.parse_args(); bundle=Path(a.bundle).resolve();out=Path(a.out).resolve();out.mkdir(parents=True,exist_ok=True)
sys.path.insert(0,str(bundle/'scripts'))
from wizard import make_server
server=make_server(out/'runs')
thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
base='http://127.0.0.1:%s' % server.server_port
checks=[]
try:
    with sync_playwright() as play:
        browser=play.chromium.launch()
        context=browser.new_context(viewport={'width':1440,'height':1000},device_scale_factor=1)
        page=context.new_page(); errors=[]; page.on('pageerror',lambda e:errors.append(str(e)))
        page.goto(base+'/#'+server.session.token)
        page.wait_for_function("!location.hash")
        expect(page.locator('#error')).to_be_hidden()
        page.screenshot(path=str(out/'01-connect.png'),full_page=True)
        page.locator('.box').screenshot(path=str(out/'01-key-panel.png'))
        page.get_by_role('button',name='先体验免费示例',exact=True).click()
        expect(page.locator('[data-step="1"]')).to_be_visible()
        page.locator('#current').fill('我在给内容创作者讲 AI 企业落地，想找到更具体的选题。')
        page.screenshot(path=str(out/'02-direction.png'),full_page=True)
        page.locator('#next1').click();expect(page.locator('[data-step="2"]')).to_be_visible()
        page.locator('#excludes').fill('卖课,招聘')
        page.screenshot(path=str(out/'03-scope.png'),full_page=True)
        page.locator('[data-step="2"]').screenshot(path=str(out/'03-scope-panel.png'))
        page.locator('#next2').click();expect(page.locator('[data-step="3"]')).to_be_visible()
        expect(page.locator('#demo-note')).to_be_visible();expect(page.locator('#live-budget')).to_be_hidden()
        page.screenshot(path=str(out/'04-confirm.png'),full_page=True)
        page.locator('#start').click();expect(page.locator('#report-link')).to_be_visible(timeout=15000)
        page.screenshot(path=str(out/'05-complete.png'),full_page=True)
        checks.append('desktop demo: connect → 3 intake rounds → report')
        with page.expect_popup() as popup:
            page.locator('#report-link').click()
        report=popup.value;report.wait_for_load_state()
        expect(report.get_by_text('离线演示：全部为合成数据。',exact=True)).to_be_visible()
        assert '未观看视频' in report.locator('body').inner_text()
        report.screenshot(path=str(out/'06-report.png'),full_page=True)
        report.screenshot(path=str(out/'06-report-first-screen.png'))
        checks.append('report: demo label, fixed evidence notes, computed results')
        csv=context.request.get(base+'/report/results.csv'); assert csv.status==200
        assert '对照中位数' in csv.text()
        checks.append('authenticated CSV export')
        unauthorized=browser.new_context()
        assert unauthorized.request.get(base+'/api/state').status==401
        unauthorized.close();checks.append('unauthenticated report/session denied')
        mobile=browser.new_context(viewport={'width':390,'height':844},device_scale_factor=1)
        phone=mobile.new_page();phone.goto(base+'/#'+server.session.token)
        expect(phone.locator('#report-link')).to_be_visible()
        phone.screenshot(path=str(out/'07-mobile.png'),full_page=True)
        assert phone.evaluate('document.documentElement.scrollWidth <= innerWidth + 1')
        checks.append('mobile 390px: no horizontal overflow')
        assert not errors, errors
        checks.append('no browser JavaScript errors')
        state=context.request.get(base+'/api/state').json()
        assert state['status']=='done' and state['summary']['demo'] is True
        assert state['summary']['qualified_count']==1 and state['summary']['pending_count']==1 and state['summary']['limited_count']==0
        checks.append('fixture truth: 1 qualified, 1 pending, no limited sample')
        # Simulate a key entry ONLY with a known fake value and inspect persisted data.
        page.locator('#again').click()
        fake='SYNTHETIC-E2E-KEY-NOT-REAL'
        page.locator('#key').fill(fake);page.locator('#live').click()
        expect(page.locator('[data-step="1"]')).to_be_visible()
        assert fake not in context.request.get(base+'/api/state').text()
        for file in (out/'runs').rglob('*'):
            if file.is_file(): assert fake.encode() not in file.read_bytes(),file
        checks.append('synthetic key absent from state response and report files')
        page.locator('#quit').click()
        expect(page.locator('main')).to_contain_text('已请求停止')
        mobile.close();context.close();browser.close()
    (out/'acceptance.json').write_text(json.dumps({'source_commit':os.getenv('GITHUB_SHA','local'),
        'runner':platform.platform(),'python':platform.python_version(),'bundle':str(bundle),
        'passed':len(checks),'failed':0,'checks':checks,'real_tikhub_calls':0,
        'real_client_sessions':0,'real_model_inference_calls':0},ensure_ascii=False,indent=2)+'\n')
    print(json.dumps({'passed':len(checks),'failed':0,'screenshots':len(list(out.glob('*.png')))},ensure_ascii=False))
finally:
    server.shutdown();server.server_close()
