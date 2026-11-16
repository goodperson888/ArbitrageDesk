#!/usr/bin/env python3
"""User-run browser audit: observe response shapes; never click or send orders."""
import argparse
import base64
import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from src.variational.schema_audit import describe, safe_path
from src.execution.browser_session import valid_debugger_address, select_market_tab
from src.common.market_data import utc_now


def write_report(path, samples):
    temporary = path.with_suffix('.tmp')
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as out:
        json.dump({'observed_at': utc_now(), 'orders_submitted_by_script': False,
                   'executable_rfq_verified': False, 'receipt_schema_verified': False,
                   'samples': samples}, out, ensure_ascii=False, indent=2)
    os.replace(temporary, path)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--open-browser', action='store_true')
    p.add_argument('--address', default='127.0.0.1:9222')
    p.add_argument('--seconds', type=int, default=120)
    args = p.parse_args()
    if not valid_debugger_address(args.address) or not 10 <= args.seconds <= 600:
        raise SystemExit('只允许 127.0.0.1:有效端口；采集时长 10–600 秒')
    if args.open_browser:
        profile = ROOT/'data/variational-browser'
        profile.mkdir(mode=0o700, parents=True, exist_ok=True)
        subprocess.Popen(['/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
            '--remote-debugging-address=127.0.0.1', '--remote-debugging-port='+args.address.split(':')[1],
            '--user-data-dir='+str(profile), '--no-first-run',
            'https://omni.variational.io/perpetual/ETH'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        input('请在新开的独立 Chrome 中登录，保留一个 ETH 官网标签页。完成后回终端按回车（不会下单）：')
    from selenium import webdriver
    options = webdriver.ChromeOptions()
    options.debugger_address = args.address
    options.set_capability('goog:loggingPrefs', {'performance':'ALL'})
    driver = None
    try:
        driver = webdriver.Chrome(options=options)
        select_market_tab(driver, 'ETH')
        driver.execute_cdp_cmd('Network.enable', {})
        print('开始只读观察。请刷新官网，填 0.004 ETH、切换买卖，并查看订单历史；不必提交订单。', flush=True)
        pending, samples, seen = {}, [], set()
        output = ROOT/'data/variational_schema_audit.json'
        until = time.monotonic()+args.seconds
        while time.monotonic() < until:
            for item in driver.get_log('performance'):
                event = json.loads(item['message'])['message']
                params, method = event.get('params', {}), event.get('method')
                request_id = params.get('requestId')
                if method == 'Network.responseReceived':
                    response = params.get('response', {})
                    path = safe_path(response.get('url', ''))
                    if path and 200 <= response.get('status', 0) < 300 and 'json' in response.get('mimeType',''):
                        pending[request_id] = path
                        if len(pending)>100: pending.pop(next(iter(pending)))
                elif method == 'Network.loadingFinished' and request_id in pending:
                    path = pending.pop(request_id)
                    if params.get('encodedDataLength',0)>1_000_000: continue
                    try:
                        raw = driver.execute_cdp_cmd('Network.getResponseBody', {'requestId':request_id})
                        body = base64.b64decode(raw['body']).decode() if raw.get('base64Encoded') else raw['body']
                        if len(body)>1_000_000: continue
                        fields = describe(json.loads(body))
                        identity = (path, tuple((f['path'],f['type']) for f in fields))
                        if identity in seen or len(samples)>=100: continue
                        seen.add(identity); samples.append({'endpoint':path,'fields':fields})
                        write_report(output,samples)
                        print('已记录字段结构：'+path, flush=True)
                    except Exception:
                        continue
            time.sleep(.25)
        write_report(output,samples)
        print('报告：'+str(output)+'；不会自动生成已验收标志。')
    finally:
        if driver: driver.service.stop()


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        print('观察已停止，浏览器保留。')
    except Exception as exc:
        raise SystemExit('观察未完成：'+type(exc).__name__+'；检查独立浏览器、调试端口和 ChromeDriver 版本。') from None
