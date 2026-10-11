#!/usr/bin/env python3
"""One read-only delivery gate: expected source, installed files, live service, report."""
import argparse
import hashlib
import http.client
import json
import sys
from pathlib import Path
from urllib.parse import urlsplit

from core import atomic_json, now, read_json
from provenance import source_identity, source_manifest
from verification import ReportHTML, audit_run


def check_delivery(source, installed, ready):
    source, installed = Path(source).resolve(), Path(installed).resolve()
    checks = []
    def check(name, passed, observed):
        checks.append({'id': name, 'status': 'PASS' if passed else 'FAIL', 'observed': observed})
    expected, actual = source_manifest(source), source_manifest(installed)
    expected_identity = source_identity(source)
    check('installed-files', bool(expected) and expected == actual,
          {'source': str(source), 'installed': str(installed), 'expected_files': len(expected),
           'different_files': sorted(key for key in set(expected) | set(actual) if expected.get(key) != actual.get(key))})
    endpoint = urlsplit(ready['url'])
    if (endpoint.scheme != 'http' or endpoint.hostname != '127.0.0.1' or not endpoint.port
            or endpoint.username or endpoint.password or not endpoint.fragment
            or endpoint.path != '/' or endpoint.query):
        raise ValueError('Expected a local wizard startup URL.')
    # No redirects, proxies, new Key reads, or paid operations.
    connection = http.client.HTTPConnection('127.0.0.1', endpoint.port, timeout=8)
    def get(path):
        connection.request('GET', path, headers={'Cookie': 'research_session=' + endpoint.fragment})
        response = connection.getresponse()
        data = response.read(16 * 1024 * 1024 + 1)
        if response.status != 200 or len(data) > 16 * 1024 * 1024:
            raise ValueError('Local verification response was missing or too large.')
        return data
    try:
        observed = json.loads(get('/api/provenance'))
        state = json.loads(get('/api/state'))
        for name in ('startup', 'disk'):
            identity = observed.get(name, {})
            check('live-' + name, identity.get('sha256') == expected_identity['sha256']
                  and identity.get('source_dir') == str(installed), identity)
        check('startup-entry', ready.get('source_dir') == str(installed)
              and ready.get('source_at_start') == observed.get('startup'),
              {'source_dir': ready.get('source_dir'), 'matches_live_startup': ready.get('source_at_start') == observed.get('startup')})
        if state.get('status') == 'running' or not state.get('output_dir'):
            check('finished-run', False, {'state': state.get('status'), 'has_output': bool(state.get('output_dir'))})
            audit = None
        else:
            root = Path(state['output_dir']).resolve()
            check('live-run-directory', observed.get('output_dir') == str(root), {'run_dir': str(root)})
            audit = audit_run(root)
            check('saved-report-audit', audit['status'] == 'PASS', {'passed': audit['passed'], 'failed': audit['failed']})
            check('live-report-ready', state.get('status') in ('done','report_ready') and (state.get('summary') or {}).get('report_complete') is True,
                  {'state': state.get('status')})
            local = (root / 'report.html').read_bytes()
            served = get('/report/report.html')
            # The server's only addition is the explicitly delimited navigation block.
            start, end = b'<!-- live-navigation:start -->', b'<!-- live-navigation:end -->'
            valid_marker = served.count(start) == served.count(end) == 1
            if valid_marker:
                a, b = served.index(start), served.index(end) + len(end)
                stripped = served[:a] + served[b:]
            else:
                stripped = served
            check('served-report', valid_marker and stripped == local,
                  {'saved_sha256': hashlib.sha256(local).hexdigest(),
                   'served_body_sha256': hashlib.sha256(stripped).hexdigest(), 'navigation_present': valid_marker})
            html = ReportHTML(); html.feed(served.decode('utf-8'))
            check('new-research-link', '/?new=1' in html.links, {'href': '/?new=1'})
        check('source-stable', source_manifest(source) == expected and source_manifest(installed) == actual,
              {'unchanged_during_check': source_manifest(source) == expected and source_manifest(installed) == actual})
    finally:
        connection.close()
    failed = sum(row['status'] == 'FAIL' for row in checks)
    return {'status': 'FAIL' if failed else 'PASS', 'scope':'installed_entry_report_delivery',
            'completion':audit['completion'] if audit else None, 'at': now(), 'passed': len(checks) - failed,
            'failed': failed, 'checks': checks, 'run_audit': audit,
            'not_checked': ['浏览器自动跳转、手机布局和阅读体验须另跑浏览器检查',
                            '其他客户端真实账户、真实付费性能、未检索范围']}


def main():
    parser = argparse.ArgumentParser(description='独立核对指定源码、实际安装、运行服务和已交付报告。')
    parser.add_argument('--source', required=True)
    parser.add_argument('--installed', required=True)
    parser.add_argument('--ready-file', required=True, help='启动器返回信息的 JSON 文件，含本机会话地址。')
    parser.add_argument('--out', required=True, help='无会话口令的检查结果 JSON。')
    args = parser.parse_args()
    try:
        result = check_delivery(args.source, args.installed, read_json(args.ready_file))
    except Exception as error:
        result = {'status': 'FAIL', 'passed': 0, 'failed': 1,
                  'checks': [{'id': 'evidence-unavailable', 'status': 'FAIL',
                              'observed': {'error_type': type(error).__name__}}],
                  'not_checked': ['验证未完成，不能视为交付通过']}
    atomic_json(Path(args.out), result)
    print(json.dumps({'status': result['status'], 'passed': result['passed'], 'failed': result['failed']}, ensure_ascii=False))
    return 0 if result['status'] == 'PASS' else 1


if __name__ == '__main__':
    sys.exit(main())
