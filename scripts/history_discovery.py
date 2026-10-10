"""Discover additional posts from already-searched small authors, with raw provenance."""
import copy
from core import read_json, within_search, stamp, digest, StopRun


def from_cache(store):
    records=[];search_ids=set();authors={};found={}
    logged={e['key'] for e in store.events if e.get('event')=='request'}
    for path in (store.root/'cache').glob('*.json'):
        record=read_json(path)
        if record.get('operation') not in ('search','posts'):continue
        if path.stem not in logged or path.stem!=digest({'op':record['operation'],'params':record['params']}):
            raise StopRun('作品发现来源与请求记录不一致。')
        records.append((record.get('captured_at',''),path.stem,record))
    for _,key,record in sorted(records):
        if record['operation']!='search':continue
        for item in record['data']['videos']:
            if within_search(item,store.config,stamp(store.state['as_of'])):search_ids.add(item['id'])
            aid=item.get('author_id')
            if aid and type(item.get('followers')) is int:
                authors[aid]=(item,key)
    for _,key,record in sorted(records):
        if record['operation']!='posts' or record['params'].get('channel')!='normal':continue
        aid=record['params'].get('sec_user_id');snapshot=authors.get(aid)
        if not snapshot:continue
        author,search_key=snapshot
        for item in record['data']['videos']:
            if (item['id'] in search_ids or item.get('author_id')!=aid or item.get('is_top')
                or not within_search(item,store.config,stamp(store.state['as_of']))):continue
            row=copy.deepcopy(item)
            # Follower snapshot belongs to this exact author, obtained by the original search.
            row['followers']=author['followers'];row['author_name']=author.get('author_name') or row.get('author_name')
            row['queries']=[];row['review']={'decision':'pending','reason':''}
            row['discovery']={'kind':'author-history','post_cache':key,'author_cache':search_key,'author_video_id':author['id']}
            found[row['id']]=row
    return found


def discover(store):
    added=[]
    for vid,row in from_cache(store).items():
        previous=store.state['candidates'].get(vid)
        if previous is None or (previous.get('discovery') and any(previous.get(k)!=v for k,v in row.items() if k!='review')):
            store.state['candidates'][vid]=row;added.append(vid)
    store.save();return added
