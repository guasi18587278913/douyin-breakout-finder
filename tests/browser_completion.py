"""Hosted UI counterexamples against a local app; synthetic fixtures, no providers."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import urllib.request

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT),str(ROOT/'scripts')]
URL='http://127.0.0.1:8892'


def serve():
    import uvicorn
    from cryptography.fernet import Fernet
    from webapp.settings import Settings
    from webapp.app import create_app
    settings=Settings(Path(os.environ['BREAKOUT_COMPLETION_TEST_DIR']),URL,Fernet.generate_key().decode(),
                      dev=True,worker_enabled=False)
    uvicorn.run(create_app(settings),host='127.0.0.1',port=8892,log_level='warning',access_log=False)


def test():
    from cryptography.fernet import Fernet
    from playwright.sync_api import sync_playwright
    from webapp.settings import Settings
    from webapp.db import Database
    from webapp.engine import run_dir
    from demo import run_demo
    from core import read_json,atomic_json
    out=ROOT/'artifacts/completion-review/browser';out.mkdir(parents=True,exist_ok=True)
    checks=[]
    def check(name,condition):
        assert condition,name
        checks.append(name)
    with tempfile.TemporaryDirectory() as temporary:
        directory=Path(temporary)
        process=subprocess.Popen([sys.executable,__file__,'serve'],cwd=ROOT,
                                 env={**os.environ,'BREAKOUT_COMPLETION_TEST_DIR':temporary})
        try:
            deadline=time.monotonic()+20
            while time.monotonic()<deadline:
                try:
                    urllib.request.urlopen(URL+'/health',timeout=1);break
                except OSError:time.sleep(.1)
            settings=Settings(directory,URL,Fernet.generate_key().decode(),dev=True,worker_enabled=False)
            db=Database(settings)
            with sync_playwright() as p:
                browser=p.chromium.launch(headless=True)
                page=browser.new_page(viewport={'width':1365,'height':1000});errors=[]
                page.on('pageerror',lambda error:errors.append(str(error)))
                page.goto(URL);page.locator('#auth-switch').click()
                page.get_by_label('用户名',exact=True).fill('completion-browser')
                page.get_by_label('密码',exact=True).fill('Synthetic-browser-password-2026')
                page.get_by_role('button',name='创建账号 →',exact=True).click()
                page.get_by_role('heading',name='小号爆款',exact=True).wait_for()
                user=page.request.get(URL+'/api/bootstrap').json()['user']
                ready=db.enqueue(user['id'],'合成反例：有报告但不足目标','all','live')
                run_demo(run_dir(settings,ready['id']),selection=True)
                db.finish(ready['id'],'done','研究已完成',{})
                missing=db.enqueue(user['id'],'合成反例：资料不足','all','live')
                folder=run_dir(settings,missing['id']);store,_=run_demo(folder,selection=True)
                for path in (folder/'cache').glob('*.json'):
                    record=read_json(path)
                    if record['operation']=='stats':
                        for row in record['data']['statistics'].values():row['play_count']=1000
                        atomic_json(path,record)
                db.finish(missing['id'],'error','写作失败',{})
                page.locator('#history-nav').click()
                page.get_by_text('合成反例：有报告但不足目标',exact=True).wait_for()
                history=page.locator('#history-list').inner_text()
                check('history distinguishes report from goal','目标未全部达成' in history and '数据不足' in history)
                check('history never repeats old success message','研究已完成' not in history)
                page.screenshot(path=str(out/'history-desktop.png'),full_page=True)
                page.goto(URL+'/#job='+ready['id']);page.wait_for_url('**/report.html')
                check('partial goal keeps direct report navigation',page.locator('.selection-report').count()==1)
                report=page.locator('main').inner_text()
                check('reader report stays free of internal completion terms','预算与数据边界' not in report and '20–30' not in report)
                page.goto(URL+'/#job='+missing['id'])
                page.get_by_text('已有资料不足以核验出可解读案例，重复整理不能补齐数据。本轮尚未完成。',exact=True).wait_for()
                check('unproductive resume hidden',not page.locator('#resume-job').is_visible())
                check('existing stage result retained',page.locator('#open-report').is_visible())
                page.set_viewport_size({'width':390,'height':844})
                check('mobile fits',page.evaluate('document.documentElement.scrollWidth<=innerWidth'))
                page.screenshot(path=str(out/'missing-data-mobile.png'),full_page=True)
                check('no browser script errors',not errors)
                browser.close()
            result={'source_commit':os.getenv('ACCEPTANCE_SHA') or os.getenv('GITHUB_SHA','local'),
                    'passed':len(checks),'failed':0,'checks':checks,'synthetic':True,'paid_calls':0}
            (out/'result.json').write_text(json.dumps(result,ensure_ascii=False,indent=2))
            print(json.dumps(result,ensure_ascii=False))
        finally:
            process.terminate()
            try:process.wait(timeout=5)
            except subprocess.TimeoutExpired:process.kill();process.wait()


if __name__=='__main__':
    serve() if len(sys.argv)>1 and sys.argv[1]=='serve' else test()
