"""Read-only, paired detector audit on ONE frozen production OHLC sample.

Run with AUDIT_DATABASE_URL in the environment; never prints database identity.
No settings, subscriptions, Telegram messages, trades or historical facts change.
Old/new subprocesses receive identical public OHLC JSON over stdin.
"""
import argparse
from collections import Counter
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import time


def child(root):
    sys.path[:0]=[str(Path(root)/"src"),root]
    from gatescope.quant.structure_detection import VERSION, proposals
    payload=json.load(sys.stdin);results=[]
    for pair in payload:
        counts=Counter();rejects=Counter();elapsed=[];anchors=0;bad=0;shapes=set()
        for end in range(max(60,len(pair['bars'])-19),len(pair['bars'])+1):
            bars=pair['bars'][:end];d={};start=time.perf_counter()
            events=proposals(bars,diagnostics=d);elapsed.append((time.perf_counter()-start)*1000)
            rejects.update(d.get('rejected',{}));bytime={b['ts']:b for b in bars}
            for e in events:
                counts[e['pattern']]+=1
                shapes.add((e['pattern'],tuple((p['time'],p['price']) for p in e['points'])))
                for p in e['points']:
                    anchors+=1;b=bytime.get(p['time']);basis=p.get('basis','').lower()
                    if not b or basis not in b or abs(p['price']-b[basis])>max(abs(p['price'])*1e-10,1e-12):bad+=1
        results.append({'symbol':pair['symbol'],'timeframe':pair['timeframe'],'through':pair['bars'][-1]['ts'],
            'prefixes':len(elapsed),'counts':dict(counts),'distinct_geometries':len(shapes),'rejections':dict(rejects),
            'anchors_checked':anchors,'anchor_errors':bad,'mean_ms':round(sum(elapsed)/len(elapsed),2),'max_ms':round(max(elapsed),2)})
    print(json.dumps({'version':VERSION,'pairs':results}))


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--old-root');parser.add_argument('--new-root');parser.add_argument('--child')
    args=parser.parse_args()
    if args.child:return child(args.child)
    import psycopg
    asof=datetime.now(timezone.utc);data=[]
    with psycopg.connect(os.environ['AUDIT_DATABASE_URL']) as conn:
        conn.execute('SET TRANSACTION READ ONLY')
        pairs=conn.execute('SELECT s.instrument_id,i.symbol,s.timeframe FROM market_sync_state s JOIN instruments i ON i.id=s.instrument_id WHERE s.active ORDER BY i.symbol,s.timeframe LIMIT 32').fetchall()
        for iid,symbol,tf in pairs:
            rows=conn.execute('''SELECT DISTINCT ON(open_time) open_time,open,high,low,close,volume FROM bars
                WHERE instrument_id=%s AND timeframe=%s AND is_final AND close_time<=%s AND available_at<=%s AND created_at<=%s
                ORDER BY open_time DESC,revision DESC LIMIT 600''',(iid,tf,asof,asof,asof)).fetchall()
            data.append({'symbol':symbol,'timeframe':tf,'bars':[{'ts':int(r[0].timestamp()),**dict(zip(('open','high','low','close','volume'),map(float,r[1:])))} for r in reversed(rows)]})
    env={k:v for k,v in os.environ.items() if not any(word in k for word in ('SECRET','KEY','TOKEN','PASSWORD','DATABASE'))}
    report={'as_of':asof.isoformat(),'scope':'last 20 closed-bar prefixes, up to 600 cached bars, active subscriptions only','models':[]}
    for root in (args.old_root,args.new_root):
        run=subprocess.run([str(Path(root)/'.venv/bin/python'),str(Path(__file__).resolve()),'--child',root],
            input=json.dumps(data),text=True,capture_output=True,env=env,timeout=180)
        if run.returncode:raise RuntimeError('detector audit subprocess failed; no private output echoed')
        report['models'].append(json.loads(run.stdout))
    print(json.dumps(report,ensure_ascii=False,indent=2))


if __name__=='__main__':main()
