#!/usr/bin/env python3
"""抖音小号爆款发现 CLI. All network commands need an explicit paid-call flag."""
import argparse
import json
import math
import sys
from decimal import Decimal
from pathlib import Path
from core import (Store, StopRun, BudgetStop, atomic_json, default_config, money,
                  read_json, validate_config)
from tikhub import TikHubClient
from workflow import (CachedClient, search, apply_review, verify, comments, annotate, write_inbox)
from report import render


def plan(c):
    validate_config(c)
    searches=len(c['keywords'])*c['max_search_pages']
    stats_per_target=math.ceil((1+c['baseline_count'])/2)
    minimum=searches+c['max_candidates']*(1+stats_per_target)
    maximum=searches+c['max_candidates']*(c['max_author_pages']+stats_per_target)
    unit=c.get('pricing',{}).get('usd_per_request',{})
    estimate=None
    try:
        estimate = (searches*money(unit['search']) + c['max_candidates']*
                    (c['max_author_pages']*money(unit['posts'])+stats_per_target*money(unit['stats'])))
    except (StopRun, KeyError):
        pass
    return {'provider':'TikHub direct','topic':c['topic'],'keywords':c['keywords'],
            'criteria':{'max_followers':c['max_followers'],'min_ratio':c['min_ratio'],
                        'baseline_count':c['baseline_count'],'min_baseline_count':c['min_baseline_count']},
            'request_limit':c['max_requests'],'budget_usd':c['budget_usd'],
            'candidate_limit':c['max_candidates'],'uncached_discovery_request_range':[minimum,maximum],
            'uncached_max_cost_usd':str(estimate) if estimate is not None else 'missing price quote',
            'note':'Assumes distinct authors and per-author batches. Real usage stops at either limit; comments and explicit retries also consume the same budget. Does not guarantee any number of qualified results.'}


def parser():
    p=argparse.ArgumentParser(description='按主题发现抖音小号异常高播放视频。Python 3.9+，无第三方运行依赖。')
    sub=p.add_subparsers(dest='command',required=True)
    a=sub.add_parser('config',help='生成待填写单价的任务配置；不联网')
    a.add_argument('--topic',required=True);a.add_argument('--keyword',action='append',required=True)
    a.add_argument('--out',required=True);a.add_argument('--budget-usd')
    a.add_argument('--followers',type=int,default=10000);a.add_argument('--ratio',type=float,default=10)
    a=sub.add_parser('plan',help='查看参数和请求估算；不联网');a.add_argument('--config',required=True)
    a=sub.add_parser('init',help='从已准备的配置创建空运行目录；不联网')
    a.add_argument('--config',required=True);a.add_argument('--run-dir',required=True)
    a=sub.add_parser('demo',help='用合成数据完整回放；不需要密钥或网络');a.add_argument('--out',required=True)
    a=sub.add_parser('review',help='导入 AI/人工的相关性判断；不联网')
    a.add_argument('--run-dir',required=True);a.add_argument('--file',required=True)
    a=sub.add_parser('annotate',help='导入 AI 内容拆解，注明实际分析依据；不联网')
    a.add_argument('--run-dir',required=True);a.add_argument('--file',required=True)
    a=sub.add_parser('report',help='重新生成 HTML/CSV；不联网');a.add_argument('--run-dir',required=True)
    for name in ['search','verify','comments']:
        a=sub.add_parser(name,help={'search':'按关键词搜索候选','verify':'核验已纳入候选的作者历史和播放量','comments':'只为入选作品取一页评论'}[name])
        a.add_argument('--run-dir',required=True)
        a.add_argument('--allow-paid',action='store_true',help='用户已同意本轮预算后才使用')
        a.add_argument('--offline',action='store_true',help='只读本轮缓存，缺失时停止；不会联网')
        a.add_argument('--retry-failed',action='store_true',help='对失败或中断的相同请求最多再试一次；仍计入预算')
        if name=='search':
            a.add_argument('--keyword',action='append');a.add_argument('--reason')
        elif name=='verify':
            a.add_argument('--channel',choices=['normal','lite'],default='normal')
        else:
            a.add_argument('--id',action='append',required=True)
    return p


def main(argv=None):
    args=parser().parse_args(argv)
    try:
        if args.command=='config':
            out=Path(args.out)
            if out.exists():raise StopRun('Configuration file already exists; choose a new path.')
            c=default_config(args.topic,args.keyword,args.budget_usd)
            c['scope']='与主题“%s”相关的内容；由宿主 AI 明确纳入和排除边界。' % args.topic
            c.update(max_followers=args.followers,min_ratio=args.ratio)
            validate_config(c);atomic_json(out,c)
            print('配置已生成。正式运行前填写已核实的接口 USD 单价、来源和时间：',out.resolve())
            return 0
        if args.command=='plan':
            print(json.dumps(plan(read_json(args.config,{})),ensure_ascii=False,indent=2));return 0
        if args.command=='init':
            store=Store.create(args.run_dir,read_json(args.config,{}))
            write_inbox(store);render(store)
            print('运行目录已创建：',store.root);return 0
        if args.command=='demo':
            from demo import run_demo
            store,summary=run_demo(args.out)
            print(json.dumps(summary,ensure_ascii=False,indent=2))
            print('离线演示报告：',store.root/'report.html');return 0
        store=Store(args.run_dir)
        with store.lock():
            if args.command=='review':apply_review(store,read_json(args.file,{}))
            elif args.command=='annotate':annotate(store,read_json(args.file,{}))
            elif args.command!='report':
                if args.offline and args.allow_paid:raise StopRun('Choose either --offline or --allow-paid.')
                client=CachedClient(store) if args.offline else TikHubClient(store,args.allow_paid,args.retry_failed)
                if args.command=='search':search(store,client,args.keyword,args.reason)
                elif args.command=='verify':verify(store,client,args.channel)
                elif args.command=='comments':comments(store,client,args.id)
            write_inbox(store)
            summary=render(store)
        print(json.dumps(summary,ensure_ascii=False,indent=2))
        print('报告：',store.root/'report.html')
        return 0
    except StopRun as e:
        print('停止：'+str(e),file=sys.stderr)
        return 2
    except OSError as e:
        print('本地文件操作失败：%s。请保留运行目录；已有请求预留仍计入预算。' % type(e).__name__,file=sys.stderr)
        return 3

if __name__=='__main__':
    raise SystemExit(main())
