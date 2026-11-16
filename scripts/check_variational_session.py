#!/usr/bin/env python3
"""User-run read-only audit of the existing browser; no login or order clicks."""
import json
import os
import sys
import time
import argparse
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from src.common.market_data import utc_now
from src.common.config_loader import ConfigLoader
from src.execution.browser_session import select_market_tab, valid_debugger_address, attach_browser
from src.variational.execution import OmniForm
from src.variational.receipt_reader import OmniResponseReader


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--refresh-account',action='store_true',help='只读 GET 刷新余额、持仓和订单；不改表单、不下单')
    args=parser.parse_args()
    from selenium import webdriver
    profile=json.loads((ROOT/'config/variational-execution.local.json').read_text())
    config=ConfigLoader(ROOT/'config/config.yaml')
    address=profile.get('browser_debugger_address')
    if not valid_debugger_address(address):
        raise ValueError('invalid_local_debugger')
    report={'observed_at':utc_now(),'orders_submitted':False,'form_changed':False,
            'profile_changed':False,'symbol':profile['symbol'],'checks':{},'errors':[]}
    driver=None
    try:
        driver=attach_browser(address)
        select_market_tab(driver,profile['symbol'])
        report['checks']['unique_market_tab']=True
        form=OmniForm(driver)
        report['checks']['input_unit']=form.assert_market(profile['symbol'])
        label=form.field('submit-button').text.strip()
        allowed={'买 '+profile['symbol'],'卖 '+profile['symbol'],'Buy '+profile['symbol'],'Sell '+profile['symbol']}
        report['checks']['submit_label']=label if label in allowed else 'unrecognized'
        report['checks']['submit_enabled']=form.field('submit-button').is_enabled()
        maximum=float(form.max_slippage_bps())
        report['checks']['site_max_slippage_bps']=maximum
        report['checks']['slippage_within_strategy']=maximum<=config.get('strategy.risk.slippage_tolerance',10)
        driver.execute_cdp_cmd('Network.enable',{})
        reader=OmniResponseReader(driver,profile['response_schema'],profile['account_id'])
        until=time.monotonic()+10
        while time.monotonic()<until:
            reader.pump()
            time.sleep(.2)
        quote=reader.quotes.get(profile['symbol'])
        if args.refresh_account:
            report['checks']['account_refresh']=reader.refresh_account_snapshots()
        report['checks']['account_sources']=reader.contract.account_diagnostics()
        report['checks']['new_quantity_quote_observed']=bool(quote)
        if quote:
            report['checks']['quote_size']=quote['size']
            report['checks']['quote_fee_bps']=quote['fee_bps']
            report['checks']['quote_limits']=quote['qty_limits']
        try:
            account=reader.contract.account()
            report['checks']['fresh_bound_account']=True
            report['checks']['pending_orders']=account['pending_orders']
            report['checks']['market_position']=account['positions'].get(profile['symbol'],'0')
            report['checks']['available_balance']=account['available_balance']
        except (ValueError,TypeError,KeyError):
            report['checks']['fresh_bound_account']=False
            report['errors'].append('account_snapshots_missing_stale_or_unbound')
    except KeyboardInterrupt:
        report['errors'].append('audit_interrupted')
    except Exception as exc:
        report['errors'].append(type(exc).__name__)
        # Fixed diagnostic labels, never raw browser errors or response content.
        message=str(exc)
        report['error_categories']=[label for label in
            ('unrecognized chrome option','detach','cannot parse','debuggerAddress',
             'chrome not reachable','session not created','only supports Chrome version') if label in message]
    finally:
        if driver: driver.service.stop()
        report['completed_at']=utc_now()
        output=ROOT/'data/variational-session-check.json'
        output.parent.mkdir(parents=True,exist_ok=True)
        fd=os.open(output,os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600)
        with os.fdopen(fd,'w') as stream:
            os.fchmod(stream.fileno(),0o600) if hasattr(os, 'fchmod') else None
            json.dump(report,stream,ensure_ascii=False,indent=2)
        print('只读检查完成，未修改表单、未下单、未修改验收配置。')
        print('报告：'+str(output))


if __name__=='__main__':
    main()
