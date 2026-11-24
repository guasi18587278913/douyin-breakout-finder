"""Explicit server maintenance; never prints provider credentials.

2026-11-24 起只能用封面搭子手机号登录：邀请链接已停用，管理员改用 grant-admin 指定。
列表和输出都不打印封面搭子的 subject。
"""
import argparse
import re
import sys
import time
from webapp.settings import Settings
from webapp.db import Database, InvalidInput

STOPPED='已改为手机号登录，邀请链接已无法使用：请让对方先用手机号登录一次，再用 python -m webapp.manage accounts 查到用户编号，用 grant-admin <用户编号> 设为管理员。'


def moment(value):
    return time.strftime('%Y-%m-%d %H:%M', time.localtime(value)) if value else '—'


def main(argv=None):
    parser=argparse.ArgumentParser(description='小号爆款服务器维护命令（时间按服务器本地时区显示）')
    commands=parser.add_subparsers(dest='command',required=True)
    for name in ('bootstrap','invite'):
        commands.add_parser(name,help='已停用').add_argument('--label',default='网站验收')
    commands.add_parser('accounts',help='列出封面搭子账号对应的本站用户')
    commands.add_parser('grant-admin',help='把绑定了封面搭子的账号设为管理员').add_argument('user_id')
    commands.add_parser('revoke-admin',help='撤销管理员').add_argument('user_id')
    args=parser.parse_args(argv)
    if args.command in ('bootstrap','invite'):
        # 不连数据库，直接报错退出。
        print(STOPPED,file=sys.stderr);return 2
    settings=Settings.from_env();db=Database(settings)
    if args.command=='accounts':
        print('用户编号\t建号时间\t最近登录\t角色\t研究数')
        for row in db.fm_accounts():
            print('\t'.join((row['id'],moment(row['created_at']),moment(row['last_login']),row['role'],str(row['jobs']))))
        return 0
    if not re.fullmatch(r'[a-f0-9]{32}',args.user_id):
        print('用户编号是 32 位小写十六进制，可以用 accounts 查到。',file=sys.stderr);return 2
    role='admin' if args.command=='grant-admin' else 'member'
    try:changed=db.set_role(args.user_id,role)
    except InvalidInput as error:
        print(str(error),file=sys.stderr);return 2
    done='已设为管理员' if role=='admin' else '已撤销管理员'
    print((done if changed else ('本来就是管理员' if role=='admin' else '本来就不是管理员'))+'：'+args.user_id)
    return 0


if __name__=='__main__':sys.exit(main())
