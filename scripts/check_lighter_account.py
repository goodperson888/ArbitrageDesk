#!/usr/bin/env python3
"""Locally prompt for a replacement API private key; verify read-only, never trade."""
import argparse
import asyncio
import getpass
import logging
import os
import sys
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from src.common.config_loader import ConfigLoader
from src.lighter.api_trader import LighterApiTrader


from src.lighter.account_check import check, ReadOnlyCheckError


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--account-index',type=int,default=750057)
    p.add_argument('--key-index',type=int,default=None)
    p.add_argument('--symbol', choices=['BTC','ETH'], default='BTC', help='只读校验的市场')
    p.add_argument('--save',action='store_true',help='校验成功后以权限 600 写入本地 .env')
    args=p.parse_args()
    if not sys.stdin.isatty():
        raise SystemExit('请在本地终端交互运行，密钥输入不回显。不要把密钥放入命令参数。')
    key_index=args.key_index if args.key_index is not None else int(input('新 API key 索引（旧索引为 4，以页面为准）：'))
    if key_index<4 or key_index>254 or args.account_index<0:
        raise SystemExit('账户或 API key 索引无效')
    config=ConfigLoader(ROOT/'config/config.yaml')
    proxy=config.get('common.network.proxy_url')
    if proxy:
        # The official native signer also makes read-only identity queries.
        for name in ('HTTP_PROXY','HTTPS_PROXY','http_proxy','https_proxy'):
            os.environ[name]=proxy
    logging.disable(logging.CRITICAL)
    key=getpass.getpass('新 API 私钥（不回显，非公钥）：').strip()
    try:
        result = asyncio.run(check(args.account_index,key_index,key,proxy,args.symbol))
        print("只读鉴权通过；未发送订单。", result["fee_status"])
    except ReadOnlyCheckError as exc:
        raise SystemExit(str(exc)) from None
    except Exception as exc:
        raise SystemExit('只读验证失败：'+type(exc).__name__+'。请核对账户、索引、密钥类型和网络；未输出密钥或原始响应。') from None
    if args.save:
        from dotenv import set_key
        path=ROOT/'.env'
        path.touch(mode=0o600,exist_ok=True);path.chmod(0o600)
        for name,value in {'LIGHTER_ACCOUNT_INDEX':str(args.account_index),
                           'LIGHTER_API_KEY_INDEX':str(key_index),'LIGHTER_API_PRIVATE_KEY':key}.items():
            set_key(path,name,value)
        path.chmod(0o600)
        print('已保存本地配置；不会启用自动交易。')

if __name__=='__main__': main()
