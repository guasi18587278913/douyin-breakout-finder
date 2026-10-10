"""Read-only comparison of a checkout, GitHub branch and optional running server."""
import argparse
import json
from pathlib import Path
import re
import subprocess

ROOT=Path(__file__).resolve().parents[1]


def command(args, cwd):
    result=subprocess.run(args,cwd=cwd,text=True,capture_output=True,timeout=30)
    if result.returncode:raise RuntimeError('Version check command failed; no repository or server files were changed.')
    return result.stdout.strip()


def inspect(root=ROOT, server=None):
    local=command(['git','rev-parse','HEAD'],root)
    branch=command(['git','branch','--show-current'],root)
    dirty=bool(command(['git','status','--porcelain'],root))
    remote=command(['git','ls-remote','--heads','origin','refs/heads/main'],root).split()
    if len(remote)!=2 or not re.fullmatch('[a-f0-9]{40}',remote[0]):raise RuntimeError('Remote main could not be verified.')
    result={'local_commit':local,'local_branch':branch,'working_tree_dirty':dirty,
            'github_main_commit':remote[0], 'local_matches_github':local==remote[0],
            'deployed_commit':None,'running_commit':None,'server_checked':False,
            'github_matches_running':False,
            'synchronized':False,'read_only':True}
    if server:
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*',server):raise ValueError('Use a configured SSH host name.')
        remote_command=("readlink -f /opt/douyin-breakout/current; "
                        "sudo readlink -f /proc/$(systemctl show douyin-breakout.service --property MainPID --value)/cwd")
        lines=command(['ssh','-o','BatchMode=yes',server,remote_command],root).splitlines()
        commits=[re.fullmatch(r'/opt/douyin-breakout/releases/([a-f0-9]{40})',line) for line in lines]
        if len(commits)!=2 or not all(commits):raise RuntimeError('Running server release could not be verified.')
        result.update(deployed_commit=commits[0].group(1),running_commit=commits[1].group(1),server_checked=True)
        result['github_matches_running']=remote[0]==result['deployed_commit']==result['running_commit']
        result['synchronized']=not dirty and local==remote[0]==result['deployed_commit']==result['running_commit']
    return result


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--server',help='Configured SSH host, such as topic-remix; reads the actual process directory.')
    parser.add_argument('--require-synchronized',action='store_true')
    args=parser.parse_args()
    try:
        result=inspect(server=args.server)
        print(json.dumps(result,ensure_ascii=False,indent=2))
        raise SystemExit(1 if args.require_synchronized and not result['synchronized'] else 0)
    except (RuntimeError,ValueError,subprocess.TimeoutExpired,OSError) as error:
        print(json.dumps({'status':'unverified','reason':str(error)},ensure_ascii=False));raise SystemExit(2)
