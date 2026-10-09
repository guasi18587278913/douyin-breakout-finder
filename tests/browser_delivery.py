"""Full delivery gate against the actual running entry. Never starts a paid task."""
import argparse
import json
import sys
from pathlib import Path
from playwright.sync_api import sync_playwright, expect

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts'))
from check_delivery import check_delivery
from core import atomic_json, read_json


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--source',required=True)
    parser.add_argument('--installed',required=True)
    parser.add_argument('--ready-file',required=True)
    parser.add_argument('--out',required=True)
    args=parser.parse_args();out=Path(args.out);out.mkdir(parents=True,exist_ok=True)
    ready=read_json(args.ready_file);checks=[];result={}
    try:
        result=check_delivery(args.source,args.installed,ready)
        if result['status']!='PASS':
            raise AssertionError('Source, runtime or report audit failed.')
        with sync_playwright() as play:
            browser=play.chromium.launch()
            context=browser.new_context(viewport={'width':1440,'height':1000})
            page=context.new_page();errors=[];page.on('pageerror',lambda e:errors.append(type(e).__name__))
            base=ready['url'].split('/#',1)[0]
            page.goto(ready['url']);expect(page).to_have_url(base+'/report/report.html',timeout=12000)
            checks.append('actual entry redirects directly to the delivered report')
            expect(page.locator('.selection-report')).to_be_visible()
            expected_count=result['run_audit']['coverage']['outcomes'].get('qualified',0)
            expect(page.locator('.selection-card')).to_have_count(expected_count)
            checks.append('actual report shows every qualified card')
            page.screenshot(path=str(out/'desktop.png'),full_page=True)
            page.set_viewport_size({'width':390,'height':844})
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth+1')
            page.screenshot(path=str(out/'mobile.png'),full_page=True)
            checks.append('mobile report has no horizontal overflow')
            page.locator('#new-research-link').click();expect(page.locator('#description')).to_be_visible()
            page.reload();expect(page.locator('#description')).to_be_visible()
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth+1')
            checks.append('new research opens the input and stays there after refresh')
            assert not errors;checks.append('no page script errors')
            context.close();browser.close()
        # Recheck source/run/report after the browser steps; a previous PASS cannot be reused.
        latest=check_delivery(args.source,args.installed,ready)
        assert latest['status']=='PASS'
        assert latest['run_audit']['inputs_sha256']==result['run_audit']['inputs_sha256']
        assert latest['run_audit']['report_sha256']==result['run_audit']['report_sha256']
        result=latest;result['browser_checks']=checks;result['passed']+=len(checks)
        result['not_checked']=['正文阅读质量、其他客户端真实账户、真实付费速度及未检索范围']
    except Exception as error:
        result.update(status='FAIL',failed=result.get('failed',0)+1,browser_checks=checks,
                      error_type=type(error).__name__)
    atomic_json(out/'acceptance.json',result)
    print(json.dumps({'status':result['status'],'passed':result.get('passed',0),'failed':result['failed']},ensure_ascii=False))
    return 0 if result['status']=='PASS' else 1


if __name__=='__main__':raise SystemExit(main())
