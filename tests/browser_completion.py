"""Hosted UI counterexamples against a local app; synthetic fixtures, no providers.

登录走本机起的假封面搭子（webapp/tests/fake_fm.py），不连真实的封面搭子。"""
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from urllib.parse import urlsplit

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT),str(ROOT/'scripts'),str(ROOT/'webapp/tests')]
URL='http://127.0.0.1:8892'


def serve():
    import uvicorn
    from cryptography.fernet import Fernet
    from webapp.settings import Settings
    from webapp.app import create_app
    from fake_fm import FakeFm
    settings=Settings(Path(os.environ['BREAKOUT_COMPLETION_TEST_DIR']),URL,Fernet.generate_key().decode(),
                      dev=True,worker_enabled=False,fm_base=os.environ['BREAKOUT_COMPLETION_FM_URL'])
    # 新用户 0 积分，用来看积分不够时的提示。后台任务关着、服务配置是假的，真实研究不会被执行。
    fake=FakeFm(settings.fm_client_secret,start_balance=0)
    fake.recharge_url=settings.fm_base+'/#/me/recharge'  # 同源的充值链接，页面应照用
    fake.serve(urlsplit(settings.fm_base).port)
    app=create_app(settings)
    app.state.vault.update({'tikhub_key':'synthetic-not-a-real-key','deepseek_key':'synthetic-not-a-real-key'})
    uvicorn.run(app,host='127.0.0.1',port=8892,log_level='warning',access_log=False)


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
        with socket.socket() as sock:
            sock.bind(('127.0.0.1',0));fm_port=sock.getsockname()[1]
        process=subprocess.Popen([sys.executable,__file__,'serve'],cwd=ROOT,
                                 env={**os.environ,'BREAKOUT_COMPLETION_TEST_DIR':temporary,
                                      'BREAKOUT_COMPLETION_FM_URL':'http://127.0.0.1:'+str(fm_port)})
        try:
            deadline=time.monotonic()+20
            while time.monotonic()<deadline:
                try:
                    urllib.request.urlopen(URL+'/health',timeout=1);break
                except OSError:time.sleep(.1)
            # 这里只用来直接往库里写合成记录；放宽每日次数只影响本测试进程写入，不影响被测服务。
            settings=Settings(directory,URL,Fernet.generate_key().decode(),dev=True,worker_enabled=False,daily_live_jobs=5,daily_user_jobs=5)
            fm_base='http://127.0.0.1:'+str(fm_port)
            db=Database(settings)
            with sync_playwright() as p:
                browser=p.chromium.launch(headless=True)
                page=browser.new_page(viewport={'width':1365,'height':1000});errors=[]
                page.on('pageerror',lambda error:errors.append(str(error)))
                page.goto(URL);page.get_by_role('link',name='用手机号登录 / 注册 →',exact=True).click()
                page.get_by_role('heading',name='小号爆款',exact=True).wait_for()
                user=page.request.get(URL+'/api/bootstrap').json()['user']
                hint=page.locator('#credit-hint')
                check('low credits explained before starting',hint.is_visible() and '你现在有 0 分' in hint.inner_text())
                page.locator('#topic').fill('合成反例：积分不够')
                with page.expect_response(lambda r:r.url.endswith('/api/jobs') and r.request.method=='POST') as answer:
                    page.locator('#research-submit').click()
                check('insufficient credits refused without a job',answer.value.status==402 and db.jobs(user['id'])==[])
                link=hint.locator('a.recharge-link')
                check('shortage shows same-origin recharge link from fm',hint.is_visible() and link.get_attribute('target')=='_blank'
                      and link.get_attribute('href')==fm_base+'/#/me/recharge')
                page.screenshot(path=str(out/'low-credits-desktop.png'),full_page=True)
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
                # 停止后已经退分的研究：接续要重新扣分，页面照实写；积分不够时给提示和充值链接
                stopped=db.enqueue(user['id'],'合成反例：停止后接续','all','live')
                db.begin_charge(stopped['id'],'0'*64,5)
                with db.connection(True) as c:c.execute("UPDATE credit_charges SET state='refunded' WHERE reference=?",(stopped['id'],))
                folder=run_dir(settings,stopped['id']);folder.mkdir(parents=True);(folder/'state.json').write_text('{}')
                db.finish(stopped['id'],'cancelled','已停止研究，已经取得的数据已保存。',{})
                page.set_viewport_size({'width':1365,'height':1000});page.goto(URL+'/#job='+stopped['id'])
                page.locator('#resume-job').wait_for(state='visible')
                check('resume cost shown next to resume',page.locator('#resume-cost').inner_text()=='这次接续会用 5 积分')
                with page.expect_response(lambda r:r.url.endswith('/resume') and r.request.method=='POST') as answer:
                    page.locator('#resume-job').click()
                resume_hint=page.locator('#resume-hint')
                resume_hint.wait_for(state='visible')
                check('resume shortage refused with recharge link',answer.value.status==402 and '你现在有 0 分' in resume_hint.inner_text()
                      and resume_hint.locator('a.recharge-link').get_attribute('href')==fm_base+'/#/me/recharge'
                      and db.job(stopped['id'])['status']=='cancelled')
                page.screenshot(path=str(out/'resume-cost-desktop.png'),full_page=True)
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
