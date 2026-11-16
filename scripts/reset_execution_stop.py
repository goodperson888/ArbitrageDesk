#!/usr/bin/env python3
"""Read-only account reconciliation, then clear a user-requested stop. No orders."""
import json
import os
import sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from src.common.config_loader import ConfigLoader
from src.execution.reset_stop import reset_stop
from src.execution.reset_stop import reconcile_unknown_orders
from src.execution.orders import decimal


def main():
    report={'success':False,'orders_submitted':False,'stage':'核对本地执行记录'}
    def verify():
        from src.execution.browser_session import attach_browser,select_market_tab
        from src.variational.receipt_reader import OmniResponseReader
        from src.lighter.execution import OfficialSDKTransport,SDKLoop,LighterExecutionAdapter
        from src.lighter.api_trader import LighterApiTrader
        config=ConfigLoader(ROOT/'config/config.yaml')
        profile=json.loads((ROOT/'config/variational-execution.local.json').read_text())
        driver=sdk=public=None
        try:
            report['stage']='只读核对 Variational 账户'
            driver=attach_browser(profile['browser_debugger_address'])
            select_market_tab(driver,profile['symbol'])
            driver.execute_cdp_cmd('Network.enable',{})
            reader=OmniResponseReader(driver,profile['response_schema'],profile['account_id'])
            reader.refresh_account_snapshots()
            account=reader.contract.account()
            if account['pending_orders'] or any(decimal(x)!=0 for x in account['positions'].values()):
                raise ValueError('Variational 非空仓或存在挂单')
            report['stage']='只读核对 Lighter 账户'
            index=int(os.environ['LIGHTER_ACCOUNT_INDEX'])
            proxy=config.get('common.network.proxy_url')
            sdk=SDKLoop(lambda:OfficialSDKTransport.from_credentials(index,int(os.environ['LIGHTER_API_KEY_INDEX']),os.environ['LIGHTER_API_PRIVATE_KEY'],proxy_url=proxy))
            settings=config.get_lighter_config()
            settings.setdefault('api',{})['proxy_url']=proxy
            public=LighterApiTrader(settings,account_index=index)
            adapter=LighterExecutionAdapter(public,sdk,None,index,enabled=False)
            adapter.assert_flat(profile['symbol'])
            report['symbol']=profile['symbol']
            return True
        finally:
            if public:public.close()
            if sdk:sdk.close()
            if driver:driver.service.stop()
    try:
        reset_stop(ROOT, verify,
                   reconcile_orders=lambda: reconcile_unknown_orders(ROOT, ['variational', 'lighter']))
        report.update(success=True,message='已解除停止；未启用实盘、未下单。请重新执行只读检查。')
    except Exception as exc:
        # Never echo SDK exceptions, credentials or browser payloads.
        message = str(exc)
        if (message.startswith('行情请求失败: ') or message.startswith('本地代理不可用或直连失败（')
                or '未配置' in message or '非空仓' in message):
            reason = message
        elif message in ('存在未核对订单；请先核对成交回执', '未确认两平台账户空仓且本次市场无挂单'):
            reason = message
        else:
            reason = type(exc).__name__ + '：账户只读核对未完成'
        report['message']=report['stage']+'未通过：'+reason+'；停止状态保留，未发送订单。'
    print(json.dumps(report,ensure_ascii=False))

if __name__=='__main__':main()
