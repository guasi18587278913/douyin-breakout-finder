"""Real browser acceptance of the installed bundle; synthetic data and key only."""
import argparse,json,os,platform,sys,threading
from pathlib import Path
from playwright.sync_api import sync_playwright,expect
p=argparse.ArgumentParser();p.add_argument('--bundle',required=True);p.add_argument('--out',required=True)
a=p.parse_args();bundle=Path(a.bundle).resolve();out=Path(a.out).resolve();out.mkdir(parents=True,exist_ok=True)
sys.path.insert(0,str(bundle/'scripts'))
from wizard import make_server
server=make_server(out/'runs');threading.Thread(target=server.serve_forever,daemon=True).start()
base='http://127.0.0.1:%s'%server.server_port;checks=[]
try:
 with sync_playwright() as play:
  browser=play.chromium.launch();context=browser.new_context(viewport={'width':1440,'height':1000},device_scale_factor=1)
  page=context.new_page();errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
  page.goto(base+'/#'+server.session.token);page.wait_for_function('!location.hash')
  expect(page.locator('#error')).to_be_hidden();page.screenshot(path=str(out/'01-home.png'),full_page=True)
  page.get_by_role('textbox',name='你想找什么样的内容？').fill('我想了解一下AI自媒体现在有的一些小号爆款。')
  page.get_by_role('button',name='帮我找爆款',exact=True).click()
  expect(page.locator('#brief-title')).to_contain_text('「AI自媒体」')
  page.screenshot(path=str(out/'02-direction.png'),full_page=True)
  checks.append('natural-language request becomes AI自媒体, with one inline follow-up')
  page.get_by_role('button',name='实战案例',exact=True).click()
  page.get_by_role('button',name='生成报告',exact=True).click()
  expect(page.locator('#key-dialog')).to_be_visible();page.screenshot(path=str(out/'03-key.png'),full_page=True)
  page.locator('#key-dialog').screenshot(path=str(out/'03-key-panel.png'))
  assert server.session.status=='ready' and server.session.run_dir is None
  checks.append('missing key opens dialog without a paid request or budget form')
  page.locator('#close-dialog').click()
  server.session.mode='demo';server.session.start({'description':'FDE'})
  page.reload()
  expect(page).to_have_url(base+'/report/report.html',timeout=15000)
  report=page
  checks.append('completed report opens directly in the same tab')
  expect(report.get_by_text('离线演示：全部为合成数据。',exact=True)).to_be_visible()
  assert '这个思路怎么借鉴' in report.locator('body').inner_text()
  assert '待核验' not in report.locator('body').inner_text()
  assert '合成' in report.locator('body').inner_text()
  report.screenshot(path=str(out/'05-report.png'),full_page=True)
  report.screenshot(path=str(out/'05-report-first-screen.png'))
  checks.append('report distinguishes evidence-only notes and synthetic data')
  response=context.request.get(base+'/report/results.csv');assert response.status==200 and '账号正常播放量' in response.text()
  checks.append('authenticated CSV export works')
  anonymous=browser.new_context();assert anonymous.request.get(base+'/api/state').status==401;anonymous.close()
  checks.append('private state requires session')
  mobile=browser.new_context(viewport={'width':390,'height':844},device_scale_factor=1)
  phone=mobile.new_page();phone.goto(base+'/#'+server.session.token);expect(phone).to_have_url(base+'/report/report.html')
  phone.screenshot(path=str(out/'06-mobile-result.png'),full_page=True)
  assert phone.evaluate('document.documentElement.scrollWidth <= innerWidth+1')
  phone.locator('#new-research-link').click();expect(phone.locator('#description')).to_be_visible();phone.screenshot(path=str(out/'07-mobile-home.png'),full_page=True)
  assert phone.evaluate('document.documentElement.scrollWidth <= innerWidth+1')
  checks.append('390px home and report have no horizontal overflow')
  phone.reload();expect(phone.locator('#description')).to_be_visible()
  assert mobile.request.get(base+'/api/state').json()['status']=='done'
  checks.append('new research remains on the input page after reload without starting queries')
  state=context.request.get(base+'/api/state').json()
  assert state['status']=='done' and state['summary']['demo'] is True
  assert state['summary']['qualified_count']==1 and state['summary']['pending_count']==1
  checks.append('fixture truth: 1 qualified, 1 pending')
  from core import Store
  from research import accept_research
  from report import render
  import copy
  store=Store(server.session.run_dir)
  full={key:copy.deepcopy(store.state['research'][key]) for key in ('format','topic_label','intro','lessons','closing','limitations')}
  full['title']=store.state['research']['headline']
  for lesson in full['lessons']:
   for case in lesson['cases']:case.pop('basis',None)
  with store.lock():store.state['research']=None;store.save();partial_summary=render(store)
  server.session.status='needs_analysis';server.session.summary=partial_summary
  page.goto(base+'/');expect(page.locator('#report-link-label')).to_have_text('查看阶段报告')
  checks.append('incomplete report stays on the progress page')
  with store.lock():accept_research(store,full);render(store)
  expect(page).to_have_url(base+'/report/report.html',timeout=8000)
  expect(page.locator('.selection-card')).to_have_count(1)
  checks.append('offline analysis completion opens the report automatically')
  page.locator('#new-research-link').click()
  page.locator('#description').fill('FDE');page.locator('#start').click();page.locator('#start').click()
  # This case checks the explicit session-only choice. Persistence has its own suite.
  if page.locator('#remember-key').is_enabled():page.locator('#remember-key').uncheck()
  page.locator('#key').fill('SYNTHETIC-E2E-KEY-NOT-REAL');page.locator('#save-key').click()
  expect(page.locator('#key-dialog')).to_be_hidden()
  assert 'SYNTHETIC-E2E-KEY-NOT-REAL' not in context.request.get(base+'/api/state').text()
  for file in (out/'runs').rglob('*'):
   if file.is_file():assert b'SYNTHETIC-E2E-KEY-NOT-REAL' not in file.read_bytes()
  checks.append('key not exposed in state, reports or files')
  assert not errors,errors;checks.append('no JavaScript runtime errors')
  response=context.request.post(base+'/api/quit',data={},headers={'X-Wizard':'1','Origin':base});assert response.status==200
  mobile.close();context.close();browser.close()
 (out/'acceptance.json').write_text(json.dumps({'source_commit':os.getenv('GITHUB_SHA','local'),
  'runner':platform.platform(),'python':platform.python_version(),'bundle':str(bundle),
  'passed':len(checks),'failed':0,'checks':checks,'real_tikhub_calls':0,
  'real_client_sessions':0,'real_model_inference_calls':0},ensure_ascii=False,indent=2)+'\n')
 print(json.dumps({'passed':len(checks),'failed':0,'screenshots':len(list(out.glob('*.png')))}))
finally:server.shutdown();server.server_close()
