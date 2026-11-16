"""Read-only account verification with fixed, non-secret diagnostic stages."""
from src.lighter.api_trader import LighterApiTrader
from src.common.market_data import utc_now


MESSAGES = {
    20: 'Lighter 公开接口不可达，请检查设置中的本地代理端口和网络',
    21: '该账户下不存在所填 API key 索引，请以 API Keys 页面为准',
    22: '官方签名器初始化失败，请确认填的是 API 私钥而非公钥',
    23: '官方签名器未通过密钥匹配检查：账户和 API key 索引已连通，但当前私钥不对应这个索引。请使用创建该索引时保存的 80 位 API 私钥，不要填公钥或钱包私钥',
    24: '本地只读认证签名失败，请核对密钥类型和本机时间',
    25: '签名后的只读订单查询失败，请检查权限、代理或账户状态',
    26: '只读校验超时，请检查代理和网络',
    27: '接口明确返回私钥不匹配：这把 API 私钥不属于所填账户/索引，请重新核对',
    28: '你填入的是平台显示的 API 公钥，不是私钥；请使用创建 API key 时保存的私钥',
    29: '只读校验出现未分类错误，未输出密钥或原始响应',
}


class ReadOnlyCheckError(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(MESSAGES[code])


async def check(account_index, key_index, key, proxy, symbol='ETH',
                base_url=None, chain_id=None, venue='lighter'):
    import lighter
    sdk = None
    public = LighterApiTrader({'api': {'proxy_url': proxy, **({'base_url': base_url} if base_url else {})},
                               'venue': venue}, account_index=account_index)
    stage = 20
    try:
        keys, _ = public.client.get('/api/v1/apikeys', {'account_index': account_index, 'api_key_index': 255})
        market, _ = public.get_market(symbol)
        if not any(k.get('api_key_index') == key_index for k in keys.get('api_keys', [])):
            raise ReadOnlyCheckError(21)
        if any((k.get('public_key') or '').lower().removeprefix('0x') == key.lower().removeprefix('0x')
               for k in keys.get('api_keys', [])):
            raise ReadOnlyCheckError(28)
        stage = 22
        kwargs = dict(url=public.base_url, account_index=account_index, api_private_keys={key_index:key})
        if chain_id is not None:
            kwargs['chain_id'] = int(chain_id)
        sdk = lighter.SignerClient(**kwargs)
        if proxy:
            sdk.api_client.rest_client.proxy = proxy
        stage = 23
        # The native CheckClient performs its own direct HTTP request and does
        # not honor the proxy used by the generated Python REST client. When a
        # local proxy is configured, the authenticated read below is the
        # authoritative check instead; an incorrect key is returned by RH as
        # an ``invalid signature`` response.
        match_error = None if proxy else sdk.check_client()
        if match_error is not None:
            # Native signer message observed with an unregistered test key.
            if 'private key does not match the one on Lighter' in str(match_error):
                raise ReadOnlyCheckError(27)
            raise ReadOnlyCheckError(stage)
        stage = 24
        token, error = sdk.create_auth_token_with_expiry(api_key_index=key_index)
        if error or not token:
            raise ReadOnlyCheckError(stage)
        stage = 25
        try:
            orders = await sdk.order_api.account_active_orders(authorization=token,
                account_index=account_index, market_id=market['market_id'], _request_timeout=10.0)
        except Exception as exc:
            if 'invalid signature' in str(exc).lower():
                raise ReadOnlyCheckError(27) from None
            raise
        data = orders.to_dict()
        if data.get('code') != 200 or not isinstance(data.get('orders'), list):
            raise ReadOnlyCheckError(stage)
        result = {'authenticated': True, 'orders_submitted': False, 'checked_at': utc_now(),
                  'account_index': account_index, 'key_index': key_index,
                  'venue': venue, 'symbol': symbol, 'active_orders': len(data['orders']),
                  'min_quote_usdc': float(market['min_quote_amount']),
                  'min_base': float(market['min_base_amount']), 'taker_fee_bps': None,
                  'fee_status': '账户费率查询未完成'}
        try:
            limits = (await lighter.AccountApi(sdk.api_client).account_limits(
                account_index=account_index, authorization=token, _request_timeout=10.0)).to_dict()
            if limits.get('code') == 200 and type(limits.get('current_taker_fee_tick')) is int:
                result['taker_fee_tick'] = limits['current_taker_fee_tick']
                # Only zero is unit-independent. Nonzero ticks need official unit verification.
                result['taker_fee_bps'] = 0 if limits['current_taker_fee_tick'] == 0 else None
                result['fee_status'] = '账户当前 Taker 费率为零' if result['taker_fee_bps'] == 0 else '非零 fee tick，换算单位待验收'
        except Exception:
            pass
        return result
    except ReadOnlyCheckError:
        raise
    except Exception:
        raise ReadOnlyCheckError(stage) from None
    finally:
        public.close()
        if sdk:
            try:
                await sdk.close()
            except Exception:
                pass
