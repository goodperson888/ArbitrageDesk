"""Internal read-only verifier. No order submission; no secrets in argv/output."""
import asyncio
import json
import logging
import os
import sys
from check_lighter_account import check
from src.lighter.account_check import ReadOnlyCheckError


def main():
    logging.disable(logging.CRITICAL)
    try:
        payload = json.loads(sys.stdin.read(4096))
        values, proxy = payload['credentials'], payload.get('proxy')
        venue = payload.get('venue', 'lighter')
        if proxy:
            for name in ('HTTP_PROXY', 'HTTPS_PROXY', 'http_proxy', 'https_proxy'):
                os.environ[name] = proxy
        prefix = 'LIGHTER_RH_' if venue == 'lighter_rh' else 'LIGHTER_'
        base_url = 'https://api.rh.lighter.xyz' if venue == 'lighter_rh' else None
        chain_id = 466324 if venue == 'lighter_rh' else None
        result = asyncio.run(check(int(values[prefix + 'ACCOUNT_INDEX']), int(values[prefix + 'API_KEY_INDEX']),
                          values[prefix + 'API_PRIVATE_KEY'], proxy, 'ETH',
                          base_url=base_url, chain_id=chain_id, venue=venue))
        print(json.dumps({'read_only_result': result}))
        return 0
    except ReadOnlyCheckError as exc:
        return exc.code
    except Exception:
        return 29


if __name__ == '__main__':
    sys.exit(main())
