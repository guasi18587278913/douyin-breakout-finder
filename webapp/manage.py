"""Explicit server maintenance; never prints provider credentials."""
import argparse
from webapp.settings import Settings
from webapp.db import Database

def main():
    parser=argparse.ArgumentParser();parser.add_argument('command',choices=['bootstrap','invite'])
    parser.add_argument('--label',default='网站验收');args=parser.parse_args()
    settings=Settings.from_env();db=Database(settings)
    if args.command=='bootstrap':
        with db.connection() as c:
            if c.execute("SELECT 1 FROM users WHERE role='admin'").fetchone():parser.error('An administrator already exists.')
            if c.execute("SELECT 1 FROM invites WHERE role='admin' AND consumed_by IS NULL AND expires_at>strftime('%s','now')").fetchone():parser.error('An active administrator invitation already exists.')
    token=db.invite(args.label,'admin' if args.command=='bootstrap' else 'member')
    print(settings.public_url+'/#invite='+token)
if __name__=='__main__':main()
