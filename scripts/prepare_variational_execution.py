#!/usr/bin/env python3
"""Prepare the configured ETH browser form and read account data; never trade."""
import json
import os
import sys
import time
import traceback
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from src.common.config_loader import ConfigLoader
from src.common.market_data import utc_now, age_seconds
from src.execution.browser_session import attach_browser, select_market_tab, open_empty_market_session
from src.execution.journal import execution_lease
from src.execution.orders import decimal
from src.variational.execution import OmniForm
from src.variational.receipt_reader import OmniResponseReader
from src.variational.schema_replay import replay_receipts


def save_report(report):
    path=ROOT/'data/variational-form-check.json'
    fd=os.open(path,os.O_CREAT|os.O_TRUNC|os.O_WRONLY,0o600)
    with os.fdopen(fd,'w') as out:
        os.fchmod(out.fileno(),0o600) if hasattr(os, 'fchmod') else None;json.dump(report,out,ensure_ascii=False,indent=2)


def prepare(profile, config):
    if profile.get('symbol')!='ETH':
        raise ValueError('form_preparation_eth_only')
    driver=None
    report={'observed_at':utc_now(),'orders_submitted':False,'symbol':'ETH','checks':{},'errors':[]}
    def stage(name):
        report['stage']=name
        save_report(report)
    try:
        stage('连接执行浏览器')
        open_empty_market_session(profile['browser_debugger_address'],'ETH')
        driver=attach_browser(profile['browser_debugger_address'])
        select_market_tab(driver,'ETH')
        driver.execute_cdp_cmd('Network.enable',{})
        form=OmniForm(driver)
        reader=OmniResponseReader(driver,profile['response_schema'],profile['account_id'])
        stage('核对账户')
        reader.refresh_account_snapshots()
        account=reader.contract.account()
        if account['pending_orders'] or any(decimal(x)!=0 for x in account['positions'].values()):
            raise ValueError('account_not_flat')
        report['checks']['account_verified']=True
        target=config.get('strategy.risk.slippage_tolerance',10)
        stage('设置 ETH 单位与滑点')
        form.configure_execution_form('ETH',target)
        report['checks'].update(input_unit='ETH',slippage_bps=target)
        # Approx. 0.26 USDC at observed prices; never submitted. Abort above 1 USDC.
        size=decimal('0.0001')
        labels={}
        for side in ('sell','buy'):
            stage('核对'+side+'表单与数量报价')
            form.prepare('ETH',side,size,False)
            end=time.monotonic()+5
            quote=None
            while time.monotonic()<end:
                reader.pump()
                candidate=reader.quotes.get('ETH')
                if candidate and decimal(candidate['size'])==size and age_seconds(candidate['updated_at'])<=5:
                    quote=candidate;break
                time.sleep(.1)
            if not quote or decimal(quote['ask'])*size>1 or quote['fee_bps']!=0:
                raise ValueError('fresh_small_quantity_quote_missing')
            form.verify('ETH',side,size,False)
            label=form.field('submit-button').text.strip()
            if label!=('买 ETH' if side=='buy' else '卖 ETH') or not form.field('submit-button').is_enabled():
                raise ValueError('submit_control_not_recognized')
            labels[side]=label
        stage('再次确认空仓')
        reader.refresh_account_snapshots()
        final=reader.contract.account()
        if final['pending_orders'] or any(decimal(x)!=0 for x in final['positions'].values()):
            raise ValueError('account_changed_during_preparation')
        report['checks'].update(submit_labels=labels,quantity_quote_verified=True,test_quantity=str(size),
            account_sources=reader.contract.account_diagnostics())
        stage('回放既有真实开平仓回执')
        history=driver.execute_async_script("""
            const done=arguments[arguments.length-1];
            if(location.origin!=='https://omni.variational.io'){done(null);return;}
            Promise.all(['/api/orders/v2','/api/trades'].map(async path=>{
                const c=new AbortController(),t=setTimeout(()=>c.abort(),4000);
                try{const r=await fetch(path,{method:'GET',credentials:'same-origin',cache:'no-store',redirect:'error',signal:c.signal});
                    if(!r.ok)throw Error('history_unavailable');
                    const body=await r.text();if(body.length>1000000)throw Error('history_too_large');return JSON.parse(body);
                }finally{clearTimeout(t);}
            })).then(done,()=>done(null));
        """)
        if not isinstance(history,list) or len(history)!=2:
            raise ValueError('历史订单或成交接口查询失败')
        report['checks']['history_fields']={name:sorted(payload.get('result',[{}])[0])
            if payload.get('result') else [] for name,payload in zip(('orders','trades'),history)}
        save_report(report)
        events=json.loads((ROOT/'tests/fixtures/variational_manual_round_trip.json').read_text())
        report['checks']['receipt_schema']=replay_receipts(profile['account_id'],events,*history)
    except Exception as exc:
        report['errors'].append(type(exc).__name__)
        frame=traceback.extract_tb(exc.__traceback__)[-1]
        report['error_location']=Path(frame.filename).name+':'+str(frame.lineno)
    finally:
        if driver: driver.service.stop()
        report['completed_at']=utc_now()
    report['ready']=not report['errors'] and report['checks'].get('quantity_quote_verified') is True
    return report


if __name__=='__main__':
    config=ConfigLoader(ROOT/'config/config.yaml')
    profile=json.loads((ROOT/'config/variational-execution.local.json').read_text())
    try:
        with execution_lease(ROOT/'data/runtime.lock'):
            report=prepare(profile,config)
    except Exception as exc:
        report={'ready':False,'orders_submitted':False,'errors':[type(exc).__name__],'checks':{},'completed_at':utc_now()}
    path=ROOT/'data/variational-form-check.json'
    fd=os.open(path,os.O_CREAT|os.O_TRUNC|os.O_WRONLY,0o600)
    with os.fdopen(fd,'w') as out:
        os.fchmod(out.fileno(),0o600) if hasattr(os, 'fchmod') else None;json.dump(report,out,ensure_ascii=False,indent=2)
    print(json.dumps(report,ensure_ascii=False))
