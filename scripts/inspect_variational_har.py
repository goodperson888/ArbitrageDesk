#!/usr/bin/env python3
"""Extract redacted API schemas from a local Chrome HAR; no network or orders."""
import argparse
import base64
import json
import os
import sys
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from src.variational.schema_audit import describe, safe_path
from src.common.market_data import utc_now


def extract(har):
    samples, seen = [], set()
    for entry in har.get('log',{}).get('entries',[]):
        request, response=entry.get('request',{}),entry.get('response',{})
        path=safe_path(request.get('url',''))
        if not path or len(samples)>=150:
            continue
        content=response.get('content',{})
        text=content.get('text','')
        if len(text)>2_000_000:
            continue
        try:
            body=base64.b64decode(text).decode() if content.get('encoding')=='base64' else text
            fields=describe(json.loads(body))
        except (ValueError,TypeError,UnicodeError):
            continue
        request_fields=[]
        try:
            post=request.get('postData',{}).get('text','')
            if len(post)<=100_000:
                request_fields=describe(json.loads(post))
        except (ValueError,TypeError):
            pass
        identity=(path,response.get('status'),tuple((f['path'],f['type']) for f in fields),
                  tuple((f['path'],f['type']) for f in request_fields))
        if identity in seen:
            continue
        seen.add(identity)
        samples.append({'endpoint':path,'http_status':response.get('status'),
                        'method':request.get('method') if request.get('method') in ('GET','POST','PUT','DELETE') else None,
                        'request_fields':request_fields,'fields':fields})
    return {'observed_at':utc_now(),'source':'local_chrome_har',
            'orders_submitted_by_script':False,'executable_rfq_verified':False,
            'receipt_schema_verified':False,'samples':samples}


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('har',type=Path)
    p.add_argument('--output',type=Path,default=ROOT/'data/variational_schema_audit.json')
    args=p.parse_args()
    if args.har.stat().st_size>100_000_000:
        raise SystemExit('HAR 超过 100 MB；请仅导出 Variational API 请求后重试')
    report=extract(json.loads(args.har.read_text()))
    if not report['samples']:
        raise SystemExit('未发现含 JSON 响应正文的 Variational 官网 API；原报告未覆盖')
    args.output.parent.mkdir(parents=True,exist_ok=True)
    tmp=args.output.with_suffix('.tmp')
    fd=os.open(tmp,os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600)
    with os.fdopen(fd,'w') as out:json.dump(report,out,ensure_ascii=False,indent=2)
    os.replace(tmp,args.output)
    print('已写入脱敏字段报告：'+str(args.output)+'；接口样本数 '+str(len(report['samples'])))
    print('未输出或转发认证头、Cookie、私钥、账户标识值及原始响应。未发单；不自动提升验收状态。')


if __name__=='__main__':
    try:main()
    except (ValueError,TypeError,KeyError,OSError) as exc:
        raise SystemExit('HAR 读取失败：'+type(exc).__name__+'；未输出原始内容') from None
