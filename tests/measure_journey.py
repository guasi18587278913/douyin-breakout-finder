"""Observe one expressly approved live website journey without changing production.

Default mode only logs in and checks readiness. Live mode needs a local approval
record binding one topic, one run, current production commit and existing caps.
Never stores cookies, passwords, request bodies, model prompts or provider keys.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import queue
import shlex
import subprocess
import threading
import time
from urllib.parse import urlsplit

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
BASE = 'https://breakout.zmtgzt.com'
COMMIT = '1e57d5274911019fe22fded94ed55894ecdddb68'


def save(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')


def observer(user_id, topic, out, preflight, source_dir):
    argv = ['sudo', '-n', 'python3', '-u', '-c', 'exec(__import__("json").loads(__import__("sys").stdin.readline()))', '--user-id', user_id,
            '--topic-sha256', hashlib.sha256(topic.encode()).hexdigest()]
    if preflight:
        argv.append('--preflight')
    process = subprocess.Popen(['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10', 'topic-remix', shlex.join(argv)],
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    process.stdin.write(json.dumps((ROOT / 'tests/observe_journey.py').read_text()) + '\n')
    process.stdin.flush()
    if preflight:
        process.stdin.close()
    ready = queue.Queue()
    def collect():
        with (out / 'server-events.jsonl').open('w') as target:
            for line in process.stdout:
                target.write(line)
                target.flush()
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                if record.get('kind') == 'observer_ready':
                    ready.put(record)
                elif record.get('kind') == 'job':
                    print(json.dumps({'server_status': record['status'], 'message': record['message'],
                                      'stage': record['progress'].get('stage')}, ensure_ascii=False), flush=True)
    thread = threading.Thread(target=collect, daemon=True)
    thread.start()
    try:
        record = ready.get(timeout=20)
    except queue.Empty:
        process.terminate()
        raise RuntimeError('Read-only server observer did not become ready.') from None
    matches = all(hashlib.sha256((source_dir / name).read_bytes()).hexdigest() == sha
                  for name, sha in record.get('source_sha256', {}).items())
    if (not record['source_dir'].endswith('/' + COMMIT) or record['runtime_source_dir'] != record['source_dir']
            or not record.get('source_sha256') or not matches
            or record['latest_source_mtime'] > record['process_started_at']):
        process.terminate()
        raise RuntimeError('Production version differs from approved measurement baseline.')
    return process, thread, record


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--credentials', type=Path, required=True)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--topic', default='AI自媒体')
    parser.add_argument('--focus', choices=['all', 'tutorial', 'case', 'opinion'], default='all')
    parser.add_argument('--approval', type=Path)
    parser.add_argument('--source-dir', type=Path, default=ROOT)
    parser.add_argument('--timeout', type=float, default=900)
    args = parser.parse_args()
    live = args.approval is not None
    if live:
        approval = json.loads(args.approval.read_text())
        expected = {'authorized': True, 'base_url': BASE, 'source_commit': COMMIT,
                    'topic': args.topic, 'focus': args.focus, 'new_jobs': 1, 'data_max_requests': 50,
                    'data_usd_limit': '0.50', 'model_usd_limit': '0.15', 'automatic_resumes': 0}
        if any(approval.get(k) != v for k, v in expected.items()):
            raise RuntimeError('Explicit test approval does not match this one-run plan.')
        consumed = args.approval.resolve().with_suffix('.consumed.json')
        if consumed.exists():
            raise RuntimeError('This one-run approval was already consumed; never resubmit it.')
    args.out.mkdir(parents=True, exist_ok=False)
    account = json.loads(args.credentials.read_text())
    origin = time.monotonic()
    marks = []
    requests = []
    by_request = {}
    errors = []
    job_responses = []
    bootstrap = {}
    allowed_to_start = False
    paid_posts = 0
    remote = thread = None
    result = {'mode': 'live' if live else 'preflight', 'topic': args.topic, 'focus': args.focus,
              'source_commit': COMMIT, 'started_at_epoch': time.time(), 'status': 'incomplete',
              'browser_cache': 'Disabled by mutation guard; fresh browser context.',
              'human_typing_time_measured': False}
    def mark(name, **fields):
        value = {'name': name, 'seconds': round(time.monotonic()-origin, 4), 'epoch': time.time(), **fields}
        marks.append(value)
        print(json.dumps(value, ensure_ascii=False), flush=True)
        return value
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            context = browser.new_context(viewport={'width': 1365, 'height': 980})
            page = context.new_page()
            def guard(route):
                nonlocal paid_posts
                req = route.request
                path = urlsplit(req.url).path
                if req.method == 'POST':
                    if path == '/api/login':
                        route.continue_()
                        return
                    expected = {'topic': args.topic, 'focus': args.focus, 'mode': 'live'}
                    if path != '/api/jobs' or not live or not allowed_to_start or paid_posts or req.post_data_json != expected:
                        errors.append('Blocked unauthorized website mutation: ' + path)
                        route.abort()
                        return
                    # Claim this approval durably before sending, including unknown/failed outcomes.
                    fd = os.open(consumed, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                    with os.fdopen(fd, 'w') as f:
                        json.dump({'approval_sha256': hashlib.sha256(args.approval.read_bytes()).hexdigest(),
                                   'claimed_at': time.time(), 'out': str(args.out.resolve()),
                                   'user_id': bootstrap['user']['id'], 'topic': args.topic}, f)
                        f.flush()
                        os.fsync(f.fileno())
                    paid_posts += 1
                    mark('research_request_sent')
                route.continue_()
            page.route(BASE + '/**', guard)
            def requested(req):
                if not req.url.startswith(BASE + '/'):
                    return
                item = {'method': req.method, 'path': urlsplit(req.url).path, 'resource_type': req.resource_type,
                        'start_seconds': round(time.monotonic()-origin, 4)}
                requests.append(item)
                by_request[id(req)] = item
            def response_received(response):
                nonlocal bootstrap
                path = urlsplit(response.url).path
                item = by_request.get(id(response.request))
                if item is not None:
                    item.update(status=response.status, response_seconds=round(time.monotonic()-origin, 4))
                try:
                    if path == '/api/bootstrap' and response.status == 200:
                        data = response.json()
                        bootstrap = {k: data.get(k) for k in ('user', 'ready', 'remaining')}
                    if (path == '/api/jobs' and response.request.method == 'POST') or path.startswith('/api/jobs/'):
                        data = response.json()
                        safe = {k: data.get(k) for k in ('id', 'status', 'message', 'created_at', 'updated_at', 'report_url', 'report_ready', 'completion', 'can_resume')}
                        progress = data.get('progress') or {}
                        safe['progress'] = {k: progress.get(k) for k in ('stage', 'candidates', 'qualified', 'elapsed_seconds', 'first_result_seconds')}
                        safe['observed_seconds'] = round(time.monotonic()-origin, 4)
                        job_responses.append(safe)
                except Exception:
                    errors.append('Could not decode timing metadata: ' + path)
            page.on('request', requested)
            page.on('response', response_received)
            page.on('requestfinished', lambda req: by_request.get(id(req), {}).update(end_seconds=round(time.monotonic()-origin, 4)))
            page.on('pageerror', lambda error: errors.append(type(error).__name__))
            mark('page_navigation_started')
            page.goto(BASE, wait_until='domcontentloaded')
            page.locator('#auth-view').wait_for(state='visible')
            mark('login_form_ready')
            page.locator('#username').fill(account['username'])
            page.locator('#password').fill(account['password'])
            mark('login_submitted')
            page.locator('#auth-submit').click()
            page.locator('#home-view').wait_for(state='visible', timeout=30000)
            mark('research_form_ready')
            if not bootstrap.get('user'):
                raise RuntimeError('Acceptance account login failed.')
            if live and approval.get('account_user_id') != bootstrap['user']['id']:
                raise RuntimeError('Acceptance account differs from the approved account.')
            result['readiness'] = {'providers_ready': bootstrap['ready'], 'remaining_user_jobs': bootstrap['remaining'],
                                   'submit_enabled': page.locator('#research-submit').is_enabled()}
            mark('observer_setup_started')
            remote, thread, server = observer(bootstrap['user']['id'], args.topic, args.out, not live, args.source_dir)
            mark('observer_setup_finished')
            result['server_preflight'] = server
            result['account_user_id'] = bootstrap['user']['id']
            mark('research_input_started')
            page.locator('#topic').fill(args.topic)
            page.locator('#focus').select_option(args.focus)
            mark('research_input_ready', note='Script input time; human thinking/typing is not measured.')
            if not live:
                page.screenshot(path=str(args.out / 'ready-desktop.png'))
                result['status'] = 'preflight_complete'
                return
            if (not result['readiness']['submit_enabled']
                    or server['global_live_jobs_last_24h'] >= server['limits']['BREAKOUT_DAILY_LIVE_JOBS']):
                raise RuntimeError('The ordinary website does not currently allow this account to start a job.')
            mark('research_clicked')
            allowed_to_start = True
            with page.expect_response(lambda r: r.url == BASE+'/api/jobs' and r.request.method == 'POST') as submitted:
                page.locator('#research-submit').click()
            if submitted.value.status != 202:
                raise RuntimeError('Website did not accept the single new job; no retry is allowed.')
            job = submitted.value.json()
            result['job_id'] = job['id']
            remote.stdin.write(json.dumps({'job_id': job['id']}) + '\n')
            remote.stdin.close()
            mark('research_accepted', job_id=job['id'])
            page.locator('#progress-view').wait_for(state='visible')
            mark('progress_visible')
            first_cards = False
            deadline = time.monotonic()+args.timeout
            while time.monotonic() < deadline:
                if '/reports/' in page.url:
                    break
                if not first_cards and page.locator('#early-cards .early-card').count():
                    mark('first_usable_card_visible')
                    first_cards = True
                if job_responses and job_responses[-1].get('status') in ('error', 'needs_analysis', 'needs_data', 'cancelled', 'interrupted'):
                    result['status'] = job_responses[-1]['status']
                    mark('research_stopped', message=job_responses[-1].get('message'))
                    page.screenshot(path=str(args.out / 'stopped-desktop.png'), full_page=True)
                    return
                page.wait_for_timeout(200)
            else:
                result['status'] = 'observation_timeout'
                return
            page.locator('.selection-report').wait_for(state='visible', timeout=30000)
            mark('report_text_visible')
            page.wait_for_load_state('load', timeout=30000)
            page.evaluate('document.fonts.ready')
            mark('report_loaded')
            result['report_url'] = page.url
            result['report_navigation'] = page.evaluate('({navigation:performance.getEntriesByType("navigation").map(n=>n.toJSON()),paint:performance.getEntriesByType("paint").map(n=>n.toJSON())})')
            mark('evidence_capture_started')
            response = context.request.get(BASE+'/reports/'+job['id']+'/summary.json')
            result['summary'] = response.json()
            result['status'] = 'measured' if result['summary'].get('report_complete') else 'partial'
            result['job_terminal_status'] = job_responses[-1].get('status') if job_responses else None
            page.screenshot(path=str(args.out / 'report-desktop.png'), full_page=True)
            page.set_viewport_size({'width': 390, 'height': 844})
            result['mobile_overflow'] = page.evaluate('document.documentElement.scrollWidth>innerWidth')
            page.screenshot(path=str(args.out / 'report-mobile.png'), full_page=True)
            mark('evidence_capture_finished')
            mark('new_research_clicked')
            page.locator('#new-research-link').click()
            page.locator('#home-view').wait_for(state='visible')
            mark('new_research_ready')
            mark('history_clicked')
            page.locator('#history-nav').click()
            page.locator('.history-row').first.wait_for(state='visible')
            mark('history_ready')
        except Exception as error:
            errors.append('Measurement error: ' + type(error).__name__)
            result['status'] = 'measurement_error'
        finally:
            if remote:
                try:
                    if not remote.stdin.closed:
                        remote.stdin.close()
                    try:
                        remote.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        remote.terminate()
                        remote.wait(timeout=3)
                    thread.join(timeout=3)
                    result['observer_exit_code'] = remote.poll()
                    observed = []
                    bad_lines = 0
                    for line in (args.out / 'server-events.jsonl').read_text().splitlines():
                        try:
                            observed.append(json.loads(line))
                        except ValueError:
                            bad_lines += 1
                    ids = {r['id'] for r in observed if r.get('kind') == 'job'}
                    result['observer_matches_submitted_job'] = bool(result.get('job_id')) and ids == {result['job_id']}
                    result['observer_finished'] = any(r.get('kind') == 'observer_finished' and r.get('job_id') == result.get('job_id') for r in observed)
                    final_records = [r for r in observed if r.get('kind') == 'final_records']
                    result['completion'] = final_records[0].get('completion') if len(final_records) == 1 else None
                    result['research_goal_met'] = bool((result['completion'] or {}).get('overall_goal_met'))
                    result['measurement_complete'] = (not bad_lines and result['observer_exit_code'] == 0 and result['observer_matches_submitted_job']
                        and result['observer_finished'] and len(final_records) == 1
                        and bool(final_records[0].get('timings')) and bool(final_records[0].get('file_sha256', {}).get('model-events.jsonl'))
                        and bool(final_records[0].get('file_sha256', {}).get('research/requests.jsonl')))
                    if bad_lines:
                        errors.append('Observer output contains an incomplete record; browser evidence retained.')
                except Exception as error:
                    result['measurement_complete'] = False
                    errors.append('Observer cleanup error: ' + type(error).__name__)
            result.update(paid_posts=paid_posts, errors=errors, elapsed_seconds=round(time.monotonic()-origin, 4))
            save(args.out / 'browser-measurement.json', result)
            save(args.out / 'browser-marks.json', marks)
            save(args.out / 'browser-requests.json', requests)
            save(args.out / 'browser-job-observations.json', job_responses)
            browser.close()
            print(json.dumps({'status': result['status'], 'paid_posts': paid_posts, 'errors': errors}, ensure_ascii=False), flush=True)
            if result['status'] not in ('preflight_complete', 'measured') or errors:
                raise SystemExit(2)
            if live and not result.get('measurement_complete'):
                raise SystemExit(3)


if __name__ == '__main__':
    main()
