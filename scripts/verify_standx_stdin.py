"""StandX wallet-auth verifier. Reads secrets only from stdin and emits a redacted report."""
import json
import os
import sys
from datetime import datetime, timezone

from src.standx.client import StandXClient, StandXCredentials, StandXError


def main():
    client = None
    try:
        payload = json.loads(sys.stdin.read(4096))
        values = payload['credentials']
        proxy = payload.get('proxy')
        if proxy:
            for name in ('HTTP_PROXY', 'HTTPS_PROXY', 'http_proxy', 'https_proxy'):
                os.environ[name] = proxy
        client = StandXClient(StandXCredentials(
            wallet_address=values['STANDX_WALLET_ADDRESS'],
            wallet_private_key=values['STANDX_WALLET_PRIVATE_KEY'],
            chain=values.get('STANDX_CHAIN', 'bsc'),
        ))
        result = client.authenticate()
        # Keep the observed public fee field only as evidence; the execution
        # gate still requires this value to be present and fresh.
        fees = {}
        for symbol in ('BTC', 'ETH'):
            info = client.query_symbol_info(symbol)
            fees[symbol] = float(info['taker_fee']) * 10000
        if len(set(fees.values())) == 1:
            result['taker_fee_bps'] = next(iter(fees.values()))
            result['fee_source'] = 'standx_symbol_info:BTC,ETH'
        else:
            # Do not apply an ETH fee to BTC (or vice versa) when the venue
            # publishes symbol-specific schedules.
            result['taker_fee_bps'] = None
            result['fee_source'] = 'symbol_fee_mismatch'
        result['orders_submitted'] = False
        result['checked_at'] = datetime.now(timezone.utc).isoformat()
        print(json.dumps({'read_only_result': result}, ensure_ascii=False))
        return 0
    except (StandXError, KeyError, ValueError, TypeError):
        return 30
    except Exception:
        return 31
    finally:
        if client:
            client.close()


if __name__ == '__main__':
    sys.exit(main())
