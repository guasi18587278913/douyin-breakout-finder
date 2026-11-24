"""Real browser journey against the hosted app locally; no external provider calls.

登录走本机起的假封面搭子（webapp/tests/fake_fm.py），不连真实的封面搭子。"""
import argparse
import json
import os
from pathlib import Path
import sys
import tempfile
import socket
import subprocess
import time
import urllib.request
from urllib.parse import urlsplit

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/'webapp/tests'))
BASE=os.environ.get('BREAKOUT_BROWSER_TEST_URL','http://127.0.0.1:8878')
FM_BASE=os.environ.get('BREAKOUT_BROWSER_FM_URL','http://127.0.0.1:8879')

def serve():
    import uvicorn
    from cryptography.fernet import Fernet
    from webapp.settings import Settings
    from webapp.app import create_app
    from fake_fm import FakeFm
    directory=Path(os.environ['BREAKOUT_BROWSER_TEST_DIR'])
    settings=Settings(directory,BASE,Fernet.generate_key().decode(),dev=True,fm_base=FM_BASE)
    FakeFm(settings.fm_client_secret).serve(urlsplit(FM_BASE).port)
    app=create_app(settings)
    uvicorn.run(app,host='127.0.0.1',port=urlsplit(BASE).port,log_level='warning',access_log=False)

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
            page.goto(BASE+'/');page.wait_for_load_state('networkidle')
            login=page.get_by_role('link',name='用手机号登录 / 注册 →',exact=True)
            check('phone login shown',login.is_visible())
            check('no password form',page.locator('#auth-view input').count()==0)
            intro=page.locator('#auth-view').inner_text()
            check('login explains credits','使用封面搭子账号登录' in intro and '新用户送 10 积分' in intro and '跑一次真实研究用 5 积分' in intro)
            check('no invitation requested','邀请码' not in intro)
            check('sample report entry kept',page.get_by_role('link',name='先看一份真实报告 →',exact=True).is_visible())
            page.screenshot(path=str(out/'login-desktop.png'),full_page=True)
            login.click()
            page.get_by_role('heading',name='小号爆款',exact=True).wait_for()
            check('credits shown after login','积分 10' in page.locator('#account').inner_text())
            recharge=page.locator('#account a.recharge-link')
            check('recharge opens fm in new window',recharge.get_attribute('target')=='_blank' and (recharge.get_attribute('href') or '').startswith(FM_BASE+'/'))
            check('research cost shown near start','这次会用 5 积分' in page.locator('#research-form').inner_text())
            check('ordinary live action disabled while unconfigured',page.locator('#research-submit').is_disabled())
            check('ordinary account has no admin navigation',not page.locator('#admin-nav').is_visible())
            check('ordinary account has member role',page.request.get(BASE+'/api/bootstrap').json()['user']['role']=='member')
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
            page.goto(BASE+'/');page.locator('#logout').click();page.locator('#auth-view').wait_for(state='visible')
            unauth=page.request.get(report_url);check('logged-out report protected',unauth.status==401)
            # 示例报告页上的“注册”链接指向 /#register：从别的页面进来，落到登录按钮上。
            page.goto('about:blank');page.goto(BASE+'/#register');page.wait_for_load_state('networkidle');page.locator('#auth-view').wait_for(state='visible')
            check('old register link lands on phone login',page.evaluate('document.activeElement && document.activeElement.id')=='fm-login' and page.url==BASE+'/')
            # 同一个浏览器再登录：假封面搭子认出是同一个账号，研究记录还在。
            page.get_by_role('link',name='用手机号登录 / 注册 →',exact=True).click();page.locator('#history-nav').wait_for(state='visible');page.locator('#history-nav').click();page.locator('.history-row').wait_for()
            check('history restored after login',page.locator('.history-row').count()==1)
            second=browser.new_page()
            second.goto(BASE+'/');second.locator('#auth-view').wait_for(state='visible')
            second.get_by_role('link',name='用手机号登录 / 注册 →',exact=True).click();second.locator('#home-view').wait_for(state='visible')
            check('second public account cannot see owner report',second.request.get(report_url).status==404)
            check('second public account has its own history',second.request.get(BASE+'/api/jobs').json()['jobs']==[])
            second.close()
            check('no browser runtime exceptions',not errors)
        finally:browser.close()
    result={'status':'PASS','source_commit':os.getenv('ACCEPTANCE_SHA') or os.getenv('GITHUB_SHA','local'),
            'passed':len(assertions),'failed':0,'assertions':assertions,'browser_errors':errors,'paid_queries':0}
    (out/'browser-local.json').write_text(json.dumps(result,ensure_ascii=False,indent=2))
    print(json.dumps(result,ensure_ascii=False))

def run():
    """One isolated local journey; always stop only the server we started."""
    with tempfile.TemporaryDirectory(prefix='breakout-browser-') as directory:
        with socket.socket() as sock:
            sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
        with socket.socket() as sock:
            sock.bind(('127.0.0.1',0));fm_port=sock.getsockname()[1]
        base='http://127.0.0.1:'+str(port)
        env={**os.environ,'BREAKOUT_BROWSER_TEST_DIR':directory,'BREAKOUT_BROWSER_TEST_URL':base,'BREAKOUT_BROWSER_FM_URL':'http://127.0.0.1:'+str(fm_port)}
        process=subprocess.Popen([sys.executable,__file__,'serve'],cwd=ROOT,env=env)
        try:
            opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
            for _ in range(100):
                if process.poll() is not None:raise RuntimeError('Local test server exited before readiness.')
                try:
                    with opener.open(base+'/health',timeout=.5) as response:
                        if response.status==200:break
                except OSError:time.sleep(.1)
            else:raise RuntimeError('Local test server did not become ready.')
            subprocess.run([sys.executable,__file__,'test'],cwd=ROOT,env=env,check=True)
        finally:
            process.terminate()
            try:process.wait(timeout=5)
            except subprocess.TimeoutExpired:process.kill();process.wait()


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('mode',choices=['serve','test','run']);args=parser.parse_args()
    {'serve':serve,'test':test,'run':run}[args.mode]()
