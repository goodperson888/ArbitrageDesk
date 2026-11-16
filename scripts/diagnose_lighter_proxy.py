"""Diagnose native signer transport with an ephemeral, unregistered key. No orders."""
import asyncio
import logging
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from src.common.config_loader import ConfigLoader


async def main():
    proxy = ConfigLoader(ROOT/'config/config.yaml').get('common.network.proxy_url')
    if proxy:
        for name in ('HTTP_PROXY','HTTPS_PROXY','http_proxy','https_proxy'):
            os.environ[name]=proxy
    logging.disable(logging.CRITICAL)
    import lighter
    sdk=None
    try:
        key, _, error=lighter.create_api_key()
        if error:
            print('临时诊断密钥生成失败');return
        sdk=lighter.SignerClient(url='https://mainnet.zklighter.elliot.ai',account_index=750057,api_private_keys={4:key})
        if proxy:sdk.api_client.rest_client.proxy=proxy
        error=sdk.check_client()
        safe=re.sub(r'[A-Fa-f0-9]{32,}','[redacted]',str(error))
        safe=re.sub(r'https?://[^\s\"]+','[endpoint]',safe)
        print('只读签名器诊断（临时未注册密钥，未使用用户私钥，未发订单）：'+safe[:500])
    except Exception as exc:
        print('诊断失败类型：'+type(exc).__name__)
    finally:
        if sdk:await sdk.close()


if __name__=='__main__':asyncio.run(main())
