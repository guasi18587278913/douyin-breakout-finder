"""Credential UI acceptance with a fake key, isolated data and no connected analyst."""
import json,sys,tempfile,threading
from pathlib import Path
from playwright.sync_api import sync_playwright,expect
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from wizard import make_server

out=Path(sys.argv[1]);out.mkdir(parents=True,exist_ok=True);checks=[]
with tempfile.TemporaryDirectory() as temp,sync_playwright() as play:
 browser=play.chromium.launch();context=browser.new_context(viewport={'width':1440,'height':1000});page=context.new_page()
 errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
 def start():
  server=make_server(temp,assistant_mode='host');threading.Thread(target=server.serve_forever,daemon=True).start()
  return server,'http://127.0.0.1:'+str(server.server_port)
 server,base=start()
 try:
  page.goto(base+'/#'+server.session.token)
  page.locator('#description').fill('AI 自媒体');page.locator('#start').click();page.locator('#start').click()
  expect(page.locator('#key-dialog')).to_be_visible();expect(page.locator('#remember-key')).to_be_checked()
  page.screenshot(path=str(out/'remember.png'))
  page.locator('#key').fill('SYNTHETIC-BROWSER-REMEMBER-KEY');page.locator('#save-key').click()
  expect(page.locator('#key-dialog')).to_be_hidden();expect(page.locator('#saved-key-status')).to_be_visible()
  expect(page.locator('#error')).to_contain_text('分析助手尚未接入')
  assert 'SYNTHETIC-BROWSER-REMEMBER-KEY' not in context.request.get(base+'/api/state').text()
  assert not list(Path(temp).glob('run-*'))
  checks.append('save is explicit and setup does not leak or query data when analyst absent')
 finally:server.session.key=None;server.shutdown();server.server_close()
 server,base=start()
 try:
  page.goto(base+'/#'+server.session.token);expect(page.locator('#saved-key-status')).to_be_visible()
  state=context.request.get(base+'/api/state').json();assert state['key_ready'] and state['key_saved']
  checks.append('new server remembers the key without filling the password field')
  page.locator('#forget-key').click();expect(page.locator('#saved-key-status')).to_be_hidden()
  page.reload();assert not context.request.get(base+'/api/state').json()['key_ready']
  checks.append('forget survives page reload and removes the stored key')
  page.locator('#description').fill('AI 自媒体');page.locator('#start').click();page.locator('#start').click()
  page.set_viewport_size({'width':390,'height':844});page.screenshot(path=str(out/'mobile-key.png'))
  assert page.evaluate('document.documentElement.scrollWidth<=innerWidth');checks.append('390px key dialog fits')
  page.locator('#remember-key').uncheck();page.locator('#key').fill('SYNTHETIC-ONE-ROUND-KEY');page.locator('#save-key').click()
  expect(page.locator('#key-dialog')).to_be_hidden();assert not server.session.credentials.exists()
  checks.append('unchecked preference leaves no saved key')
  assert not errors;checks.append('no JavaScript runtime errors')
 finally:server.session.key=None;server.shutdown();server.server_close();browser.close()
result={'passed':len(checks),'failed':0,'checks':checks,'real_keys_used':0,'paid_queries':0}
(out/'acceptance.json').write_text(json.dumps(result,ensure_ascii=False,indent=2));print(json.dumps(result,ensure_ascii=False))
