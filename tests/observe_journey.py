"""Read-only production timing observer; stream this file over SSH, never install it.

Only a new job belonging to the specified acceptance account is observed. No app
imports, provider calls, database writes, or recovery actions. The service env
file is read for daily quota fields; credential values are never exported.
"""
import argparse
from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time


def read(path, default=None):
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def emit(kind, **data):
    print(json.dumps({'kind': kind, 'observed_at': time.time(), **data}, ensure_ascii=False), flush=True)


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None


def collect(root):
    research = root / 'research'
    state = read(research / 'state.json', {})
    config = read(research / 'config.json', {})
    checks = read(research / 'verification.json', {})
    emit('final_records',
         config={k: config.get(k) for k in ('topic', 'scope', 'keywords', 'max_requests', 'max_candidates',
               'budget_usd', 'stats_strategy', 'min_ratio', 'max_followers', 'baseline_count',
               'min_baseline_count', 'search_days', 'min_age_days', 'comparison_window_days',
               'max_search_pages', 'max_author_pages', 'content_review_required', 'reader_copy_required', 'pricing')},
         timings=read(research / 'timings.json', {}),
         summary=read(research / 'summary.json', {}),
         audit={k: checks.get(k) for k in ('status', 'passed', 'failed')},
         audit_scope=checks.get('scope'), completion=checks.get('completion'),
         audit_failures=[c for c in checks.get('checks', []) if c.get('status') == 'FAIL'],
         counts={'candidates': len(state.get('candidates', {})), 'authors': len(state.get('authors', {})),
                 'statistics': len(state.get('stats', {})),
                 'review_decisions': dict(Counter(v.get('review', {}).get('decision') for v in state.get('candidates', {}).values())),
                 'result_statuses': dict(Counter(v.get('status') for v in state.get('results', {}).values()))},
         queries={k: {field: value.get(field) for field in ('pages', 'complete', 'error', 'warning', 'reason')}
                  for k, value in state.get('queries', {}).items()},
         stops=state.get('stops', []),
         search_batches=state.get('search_batches', []),
         file_sha256={str(p.relative_to(root)): digest(p) for p in
                      (root / 'model-events.jsonl', research / 'requests.jsonl', research / 'state.json',
                       research / 'config.json', research / 'report.html', research / 'verification.json')})
    for path in sorted((root / 'model-diagnostics').glob('*validation.json')):
        emit('model_validation', file=path.name, validation=read(path, {}))
    for path in sorted((root / 'model-diagnostics').glob('*output-error.json')):
        emit('model_output_error', file=path.name, detail=read(path, {}))
    for path in sorted(research.glob('content-review-*.json')):
        record = read(path, {})
        emit('content_review', file=path.name, passed=record.get('passed'), revision=record.get('revision'),
             targets=len(record.get('packet', {}).get('targets', [])),
             failed_checks=[{k: c.get(k) for k in ('path', 'criterion', 'reason')}
                            for c in record.get('verdict', {}).get('checks', []) if c.get('pass') is False])


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--user-id', required=True)
    parser.add_argument('--topic-sha256', required=True)
    parser.add_argument('--data-dir', default='/var/lib/douyin-breakout')
    parser.add_argument('--timeout', type=float, default=1200)
    parser.add_argument('--interval', type=float, default=.2)
    parser.add_argument('--preflight', action='store_true')
    args = parser.parse_args()
    directory = Path(args.data_dir)
    db = sqlite3.connect('file:' + str(directory / 'site.sqlite3') + '?mode=ro', uri=True, timeout=2)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA query_only=ON')
    before = {r[0] for r in db.execute('SELECT id FROM jobs WHERE user_id=?', (args.user_id,))}
    started = time.monotonic()
    code = Path('/opt/douyin-breakout/current').resolve()
    pid = subprocess.check_output(['systemctl', 'show', 'douyin-breakout', '-p', 'MainPID', '--value'], text=True).strip()
    files = [p for folder in ('scripts', 'webapp') for p in (code / folder).glob('*.py')]
    files += [code / 'references/report-standard.md', code / 'references/report-example.json']
    files += [p for folder in ('assets', 'webapp/static') for p in (code / folder).iterdir()
              if p.suffix in ('.html', '.js', '.css')]
    boot = next(int(line.split()[1]) for line in Path('/proc/stat').read_text().splitlines() if line.startswith('btime '))
    ticks = int(Path('/proc/' + pid + '/stat').read_text().rsplit(') ', 1)[1].split()[19])
    process_started_at = boot + ticks / os.sysconf('SC_CLK_TCK')
    limits = {'BREAKOUT_DAILY_LIVE_JOBS': 2, 'BREAKOUT_DAILY_USER_JOBS': 2}
    for line in Path('/etc/douyin-breakout.env').read_text().splitlines():
        if '=' in line:
            key, value = line.split('=', 1)
            if key in limits:
                limits[key] = int(value.strip().strip('"'))
    emit('observer_ready', source_dir=str(code), runtime_source_dir=str(Path('/proc/' + pid + '/cwd').resolve()),
         source_sha256={str(p.relative_to(code)): digest(p) for p in files}, limits=limits,
         process_started_at=process_started_at, latest_source_mtime=max(p.stat().st_mtime for p in files),
         interval_seconds=args.interval, existing_account_jobs=len(before),
         active_jobs=db.execute("SELECT count(*) FROM jobs WHERE status IN ('queued','running')").fetchone()[0],
         global_live_jobs_last_24h=db.execute("SELECT count(*) FROM jobs WHERE mode='live' AND created_at>?", (time.time()-86400,)).fetchone()[0])
    if args.preflight:
        return
    # The browser sends its exact accepted ID; never infer another user's/task's job.
    control = sys.stdin.readline()
    if not control:
        emit('observer_finished', job_id=None, status='no_job_submitted')
        return
    job_id = json.loads(control)['job_id']
    selected = db.execute('SELECT id,topic,mode FROM jobs WHERE id=? AND user_id=?', (job_id, args.user_id)).fetchone()
    if (not selected or job_id in before or selected['mode'] != 'live'
            or hashlib.sha256(selected['topic'].encode()).hexdigest() != args.topic_sha256):
        emit('observer_error', reason='Submitted job does not match the authorized account and topic.')
        return
    previous = None
    positions = {'data': 0, 'model': 0}
    while time.monotonic() - started < args.timeout:
        if job_id:
            row = dict(db.execute('SELECT id,status,message,progress_json,created_at,updated_at,finished_at,resume_only FROM jobs WHERE id=? AND user_id=?', (job_id, args.user_id)).fetchone())
            progress = json.loads(row.pop('progress_json'))
            row['progress'] = {k: progress.get(k) for k in ('stage', 'candidates', 'qualified', 'elapsed_seconds', 'first_result_seconds')}
            if row != previous:
                emit('job', **row)
                previous = row
            root = directory / 'runs' / job_id
            for kind, path in [('data', root / 'research/requests.jsonl'), ('model', root / 'model-events.jsonl')]:
                lines = path.read_text().splitlines() if path.is_file() else []
                for line in lines[positions[kind]:]:
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        break  # Re-read an unfinished append on the next observation.
                    positions[kind] += 1
                    fields = ('event', 'operation', 'key', 'at', 'reserved_usd', 'retry', 'error') if kind == 'data' else (
                        'kind', 'id', 'stage', 'usd', 'at', 'model', 'request_hash', 'input_bound', 'max_tokens',
                        'operation', 'outcome', 'usage', 'failure_type', 'response_complete', 'provider_response_received')
                    safe = {k: event[k] for k in fields if k in event}
                    if kind == 'data':
                        params = event.get('params', {})
                        safe['request_shape'] = {k: params[k] for k in ('keyword', 'count', 'cursor', 'max_cursor') if k in params}
                        if 'aweme_ids' in params:
                            safe['request_shape']['video_id_count'] = len(params['aweme_ids'].split(','))
                    emit(kind + '_event', record=safe)
            if row['status'] not in ('queued', 'running'):
                collect(root)
                emit('observer_finished', job_id=job_id, status=row['status'])
                return
        time.sleep(args.interval)
    emit('observer_timeout', job_id=job_id, action='Observation ended; no task was cancelled or retried.')


if __name__ == '__main__':
    main()
