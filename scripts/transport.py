"""HTTP via the system curl client, as supported by TikHub's official examples.
Secrets are passed on stdin, never in command arguments, files or error messages.
"""
import json
import shutil
import subprocess
import urllib.error
import urllib.request
from core import APIError


def request(url, method='GET', payload=None, api_key=None, timeout=30, limit=32*1024*1024):
    curl = shutil.which('curl')
    if not curl:
        raise APIError('缺少网络组件 curl。请让智能体安装系统 curl 后重新打开。')
    options = ['url = '+json.dumps(url), 'request = '+json.dumps(method), 'header = "Accept: application/json"']
    if api_key:
        options.append('header = '+json.dumps('Authorization: Bearer '+api_key))
    if payload is not None:
        options += ['header = "Content-Type: application/json"',
                    'data-binary = '+json.dumps(json.dumps(payload,ensure_ascii=False),ensure_ascii=False)]
    command = [curl, '--disable', '--config', '-', '--silent', '--show-error', '--max-time', str(timeout),
               '--max-filesize', str(limit), '--proto', '=https', '--write-out', '\n%{http_code}']
    try:
        result = subprocess.run(command, input=('\n'.join(options)+'\n').encode(),
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout+3)
    except (OSError, subprocess.TimeoutExpired):
        raise APIError('网络请求没有完成，请检查网络后重试；已发出的查询可能已计费。')
    if result.returncode:
        raise APIError('网络请求未完成（连接、超时或响应大小问题）；已有查询预留会保留。')
    if len(result.stdout)>limit+5:
        raise APIError('服务响应超过大小限制。')
    body, _, status = result.stdout.rpartition(b'\n')
    try:
        status = int(status)
    except ValueError:
        raise APIError('网络组件返回了未知状态。')
    if 300 <= status < 400:
        raise APIError('数据服务发生重定向，未向其他地址转发 Key。')
    return status, body
