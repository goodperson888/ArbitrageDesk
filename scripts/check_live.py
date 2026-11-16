#!/usr/bin/env python3
"""Read-only live-feed smoke test against the local dashboard. No orders."""
import argparse
import sys
import time
import requests

parser=argparse.ArgumentParser(description=__doc__)
parser.add_argument('--seconds',type=int,default=12)
parser.add_argument('--symbol',choices=['BTC','ETH','US100'],default='BTC')
args=parser.parse_args()
session=requests.Session();session.trust_env=False
samples=[]
for _ in range(max(2,min(args.seconds,30))):
    r=session.get('http://127.0.0.1:5001/api/dashboard',params={'symbol':args.symbol},timeout=5)
    r.raise_for_status();samples.append(r.json());time.sleep(1)
for name in ['variational','lighter']:
    first=samples[0]['feeds'][name];last=samples[-1]['feeds'][name]
    print(name,'connected=',last['connected'],'messages_delta=',last['messages']-first['messages'],
          'reconnects=',last['reconnects'],'error=',last['error'])
print('distinct_lighter_book_offsets=',len({s['book'].get('offset') for s in samples if s.get('book')}))
print('distinct_variational_mark_prices=',len({s['variational'] for s in samples}))
print('quote_age_seconds=',samples[-1]['comparison']['variational_quote_age'])
print('No orders submitted. Bid/ask reference status:',samples[-1]['comparison']['reasons'])
if not all(samples[-1]['feeds'][name]['connected'] for name in ['variational','lighter']):
    sys.exit(1)
