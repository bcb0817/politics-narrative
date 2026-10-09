"""Send an existing snapshot to the owner-private Site; never run paid search."""
import getpass
import json
import urllib.request
from .social_radar import ROOT
from .article_generation import load

URL='https://social-radar-monitor.bcb0817.chatgpt.site/api/hunter'


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def sync(token, report, opener=None):
    opener=opener or urllib.request.build_opener(NoRedirect())
    headers={'OAI-Sites-Authorization':'Bearer '+token,'Content-Type':'application/json'}
    body=json.dumps(report,ensure_ascii=False).encode()
    with opener.open(urllib.request.Request(URL,data=body,headers=headers,method='POST'),timeout=60) as response:
        saved=json.load(response)
    if saved.get('run_id')!=report['run_id'] or saved.get('saved') is not True:
        raise ValueError('write_not_confirmed')
    with opener.open(urllib.request.Request(URL,headers=headers),timeout=60) as response:
        current=json.load(response)
    if (current.get('run_id')!=report['run_id'] or
        current.get('actual_cost_usd')!=report['actual_cost_usd'] or
        len(current.get('topics',[]))!=len(report['topics']) or
        current.get('status')!=report['status']):
        raise ValueError('readback_mismatch')
    for old,new in zip(report['topics'],current['topics']):
        if any(new.get(k)!=old.get(k) for k in ('title','score','source_urls','score_parts')):
            raise ValueError('candidate_readback_mismatch')
    return {'saved':True,'run_id':report['run_id'],'candidates':len(report['topics']),'status':report['status']}


def main():
    token=getpass.getpass('Site service credential (hidden): ')
    try:
        print(json.dumps(sync(token,load(ROOT/'outputs/social_radar/hunter/latest.json'))))
        return 0
    except Exception as exc:
        print(json.dumps({'saved':False,'error_type':type(exc).__name__}))
        return 2


if __name__=='__main__':
    raise SystemExit(main())
