"""Real browser journey against the hosted app locally; no external provider calls."""
import argparse
import json
import os
from pathlib import Path
import sys
import tempfile

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))

def serve():
    import uvicorn
    from cryptography.fernet import Fernet
    from webapp.settings import Settings
    from webapp.app import create_app
    directory=Path(os.environ['BREAKOUT_BROWSER_TEST_DIR'])
    settings=Settings(directory,'http://127.0.0.1:8878',Fernet.generate_key().decode(),dev=True)
    app=create_app(settings)
    uvicorn.run(app,host='127.0.0.1',port=8878,log_level='warning',access_log=False)

def test():
    from playwright.sync_api import sync_playwright
    directory=Path(os.environ['BREAKOUT_BROWSER_TEST_DIR'])
    out=ROOT/'artifacts/web-acceptance';out.mkdir(parents=True,exist_ok=True)
    assertions=[];errors=[]
    def check(name,okay):
        assert okay,name
        assertions.append(name)
    with sync_playwright() as p:
        browser=p.chromium.launch(headless=True)
        try:
            page=browser.new_page(viewport={'width':1365,'height':1000})
            page.on('pageerror',lambda error:errors.append(str(error)))
            page.goto('http://127.0.0.1:8878/');page.wait_for_load_state('networkidle');page.locator('#auth-switch').click()
            check('public registration shown',page.get_by_role('heading',name='创建账号',exact=True).is_visible())
            check('no invitation requested','邀请码' not in page.locator('#auth-view').inner_text())
            page.screenshot(path=str(out/'registration-desktop.png'),full_page=True)
            page.get_by_label('用户名',exact=True).fill('browser-owner')
            page.get_by_label('密码',exact=True).fill('Synthetic-browser-password-2026')
            page.get_by_role('button',name='创建账号 →',exact=True).click()
            page.get_by_role('heading',name='小号爆款',exact=True).wait_for()
            check('ordinary live action disabled while unconfigured',page.locator('#research-submit').is_disabled())
            check('ordinary account has no admin navigation',not page.locator('#admin-nav').is_visible())
            check('ordinary account has member role',page.request.get('http://127.0.0.1:8878/api/bootstrap').json()['user']['role']=='member')
            page.screenshot(path=str(out/'home-desktop.png'),full_page=True)
            page.set_viewport_size({'width':390,'height':844});page.screenshot(path=str(out/'home-mobile.png'),full_page=True)
            check('mobile home no horizontal overflow',page.evaluate('document.documentElement.scrollWidth<=window.innerWidth'))
            page.locator('#demo-start').click()
            page.wait_for_url('**/reports/**/report.html',timeout=30000)
            report_url=page.url
            check('completion opens actual report',page.locator('#new-research-link').is_visible())
            check('synthetic report labelled', '合成' in page.locator('body').inner_text())
            check('mobile report no horizontal overflow',page.evaluate('document.documentElement.scrollWidth<=window.innerWidth'))
            page.screenshot(path=str(out/'report-mobile.png'),full_page=True)
            page.locator('#new-research-link').click();page.get_by_role('heading',name='小号爆款',exact=True).wait_for()
            page.locator('#history-nav').click();page.locator('.history-row').wait_for()
            check('history stores completed report',page.locator('.history-row').count()==1)
            page.get_by_role('link',name='阅读报告 ↗').click();page.wait_for_url(report_url)
            page.goto('http://127.0.0.1:8878/');page.locator('#logout').click();page.locator('#auth-view').wait_for(state='visible')
            unauth=page.request.get(report_url);check('logged-out report protected',unauth.status==401)
            page.get_by_label('用户名',exact=True).fill('browser-owner');page.get_by_label('密码',exact=True).fill('Synthetic-browser-password-2026')
            page.get_by_role('button',name='登录 →',exact=True).click();page.locator('#history-nav').wait_for(state='visible');page.locator('#history-nav').click();page.locator('.history-row').wait_for()
            check('history restored after login',page.locator('.history-row').count()==1)
            second=browser.new_page()
            second.goto('http://127.0.0.1:8878/');second.locator('#auth-view').wait_for(state='visible');second.locator('#auth-switch').click()
            second.get_by_label('用户名',exact=True).fill('browser-other');second.get_by_label('密码',exact=True).fill('Synthetic-other-password-2026');second.locator('#auth-submit').click();second.locator('#home-view').wait_for(state='visible')
            check('second public account cannot see owner report',second.request.get(report_url).status==404)
            check('second public account has its own history',second.request.get('http://127.0.0.1:8878/api/jobs').json()['jobs']==[])
            second.close()
            check('no browser runtime exceptions',not errors)
        finally:browser.close()
    result={'status':'PASS','passed':len(assertions),'failed':0,'assertions':assertions,'browser_errors':errors,'paid_queries':0}
    (out/'browser-local.json').write_text(json.dumps(result,ensure_ascii=False,indent=2))
    print(json.dumps(result,ensure_ascii=False))

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('mode',choices=['serve','test']);args=parser.parse_args()
    serve() if args.mode=='serve' else test()
