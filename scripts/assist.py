#!/usr/bin/env python3
"""Host-agent interface. All commands are local/offline; none can spend API budget."""
import argparse
import base64
import json
from pathlib import Path
from core import Store, StopRun, atomic_json, read_json
from bridge import next_request,submit,bounded_json
from research import analysis_packet,review_packet,accept_review,accept_research,add_evidence,asset_lock
from workflow import CachedClient,verify,write_inbox
from report import render


def main():
    p=argparse.ArgumentParser(description='宿主智能体研究助手；只处理本地交接，不读取 Key。')
    sub=p.add_subparsers(dest='command',required=True)
    n=sub.add_parser('next');n.add_argument('--bridge',required=True);n.add_argument('--after');n.add_argument('--wait',type=int,default=50)
    n=sub.add_parser('submit');n.add_argument('--bridge',required=True);n.add_argument('--request-id',required=True);n.add_argument('--file',required=True)
    n=sub.add_parser('feedback');n.add_argument('--bridge',required=True)
    n=sub.add_parser('packet');n.add_argument('--run-dir',required=True)
    n=sub.add_parser('evidence');n.add_argument('--run-dir',required=True);n.add_argument('--id',required=True);n.add_argument('--file',required=True);n.add_argument('--kind',choices=['transcript','video-notes'],required=True);n.add_argument('--source-note',required=True)
    n=sub.add_parser('cover');n.add_argument('--run-dir',required=True);n.add_argument('--id',required=True);n.add_argument('--file',required=True)
    n=sub.add_parser('repair-offline');n.add_argument('--run-dir',required=True);n.add_argument('--review',required=True);n.add_argument('--analysis')
    n=sub.add_parser('write-report');n.add_argument('--run-dir',required=True);n.add_argument('--file',required=True)
    a=p.parse_args()
    try:
        if a.command=='next':result=next_request(a.bridge,a.after,a.wait)
        elif a.command=='submit':result=submit(a.bridge,a.request_id,bounded_json(a.file))
        elif a.command=='feedback':result=read_json(Path(a.bridge)/'feedback.json',{'accepted':None})
        else:
            store=Store(a.run_dir)
            if a.command=='packet':result=analysis_packet(store)
            elif a.command=='evidence':result=add_evidence(store,a.id,a.file,a.kind,a.source_note)
            elif a.command=='cover':
                if a.id not in store.state['candidates']:raise StopRun('封面必须对应本轮视频。')
                source=Path(a.file)
                if not source.is_file() or source.stat().st_size>8*1024*1024:raise StopRun('封面缺失或超过8MiB。')
                raw=source.read_bytes()
                mime='image/png' if raw.startswith(b'\x89PNG\r\n\x1a\n') else 'image/jpeg' if raw.startswith(b'\xff\xd8\xff') else 'image/webp' if raw.startswith(b'RIFF') and raw[8:12]==b'WEBP' else None
                if not mime:raise StopRun('只接受已下载的 PNG/JPEG/WebP 原封面。')
                with asset_lock(store):
                    covers=read_json(store.root/'covers.json',{})
                    covers[a.id]={'mime':mime,'base64':base64.b64encode(raw).decode('ascii')}
                    atomic_json(store.root/'covers.json',covers)
                result={'saved':a.id}
            else:
                with store.lock():
                    if a.command=='repair-offline':
                        answer=bounded_json(a.review)
                        if answer.get('next_keywords'):raise StopRun('离线修复不能发起补搜。')
                        accept_review(store,answer)
                        if any(v['review']['decision']=='include' for v in store.state['candidates'].values()):verify(store,CachedClient(store))
                        if a.analysis:accept_research(store,bounded_json(a.analysis))
                    else:accept_research(store,bounded_json(a.file))
                    write_inbox(store);result=render(store)
        print(json.dumps(result,ensure_ascii=False,indent=2));return 0
    except (StopRun,OSError,ValueError) as error:
        print(json.dumps({'error':str(error)},ensure_ascii=False));return 2


if __name__=='__main__':raise SystemExit(main())
