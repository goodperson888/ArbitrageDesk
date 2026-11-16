"""Read-only preparation of new signed venues, outside the execution loop."""
from decimal import Decimal
from dotenv import dotenv_values

from src.common.config_loader import ConfigLoader
from src.execution.live_scope import ScopePreparationError


def build_scope_adapter(root, venue, journal):
    from src.web import local_credentials
    from src.execution.platform_clients import LighterCredentials, build_lighter_session
    from src.lighter.execution import LighterExecutionAdapter
    from src.arcus.execution import ArcusClient, ArcusExecutionAdapter
    config = ConfigLoader(root/'config/config.yaml')
    values = dotenv_values(root/'.env')
    required = local_credentials.names_for(venue)
    if any(not values.get(key) for key in required):
        raise ScopePreparationError('新增平台尚未保存完整密钥，请先在设置中验证')
    proxy = config.get('common.network.proxy_url')
    # Reuse the existing isolated, read-only credential verifier. No key,
    # fee proof or enabled flag is changed in the dashboard's files.
    report = local_credentials.verify(root, {k:values[k] for k in required}, proxy, venue,
        **({'arcus_base_url':config.get('arcus.api.base_url', 'https://api.arcus.xyz')} if venue == 'arcus' else {}))
    fee = report.get('taker_fee_bps')
    if (report.get('authenticated') is not True or report.get('orders_submitted') is not False or
            type(fee) not in (int, float) or not Decimal(str(fee)).is_finite()):
        raise ScopePreparationError('新增平台鉴权或实际费率未核验，保留原新仓范围')
    if venue in ('lighter', 'lighter_rh'):
        prefix = 'LIGHTER_RH_' if venue == 'lighter_rh' else 'LIGHTER_'
        credentials = LighterCredentials(int(values[prefix+'ACCOUNT_INDEX']),
            int(values[prefix+'API_KEY_INDEX']), values[prefix+'API_PRIVATE_KEY'])
        public, sdk = build_lighter_session(venue, credentials, proxy)
        adapter = LighterExecutionAdapter(public, sdk, journal, credentials.account_index,
                                          enabled=True, venue=venue)
        adapter.verified_fee_bps = fee
        return adapter, [public, sdk]
    client = ArcusClient(values['ARCUS_WALLET_ADDRESS'], int(values['ARCUS_ACCOUNT_INDEX']),
        values['ARCUS_API_SIGNING_KEY'],
        base_url=config.get('arcus.api.base_url', 'https://api.arcus.xyz'),
        ws_url=config.get('arcus.api.ws_url', 'wss://api.arcus.xyz/v1/ws'),
        proxy_url=config.get('arcus.api.proxy_url', proxy),
        order_timeout=config.get('arcus.execution.order_timeout_seconds', 15))
    return ArcusExecutionAdapter(client, journal, values['ARCUS_WALLET_ADDRESS'],
                                  enabled=True, fee_bps=fee), [client]


def assert_new_account_flat(adapter, state):
    """An added venue must not silently adopt unrelated external exposure."""
    if adapter.venue in ('lighter', 'lighter_rh'):
        nonzero = any(Decimal(str(row['position'])) != 0 for row in state['account']['positions'])
        orders = state['orders']
    else:
        from src.arcus.execution import _row_value, normalize_order_update
        rows = state['positions']['positions']
        rows = rows.values() if isinstance(rows, dict) else rows
        nonzero = any(Decimal(str(_row_value(row, 'positionSize', 'size', 'quantity', 'qty')
                                 if _row_value(row, 'positionSize', 'size', 'quantity', 'qty') is not None
                                 else row['positionValueNotional'])) != 0 for row in rows)
        orders = [row for row in adapter._checked_order_rows(state['orders'])
                  if normalize_order_update(row).get('status') not in ('filled', 'partial', 'canceled', 'rejected')]
    if nonzero or orders:
        raise ScopePreparationError('新增平台有外部持仓或活动订单，未切换；请先核对该平台账户')


class ScopePreparer:
    def __init__(self, root, journal, adapters, clients, started_feeds, strategy):
        self.root, self.journal = root, journal
        self.strategy = strategy
        self.adapters, self.clients, self.started_feeds = adapters, clients, started_feeds
        self.by_venue = {adapter.venue:adapter for adapter in adapters}

    def __call__(self, request):
        created, resources = [], []
        try:
            selected = []
            for venue in request['venues']:
                trader = self.by_venue.get(venue)
                if trader is None:
                    trader, handles = build_scope_adapter(self.root, venue, self.journal)
                    created.append(trader)
                    resources.extend(handles)
                    # One background account snapshot validates the added
                    # account. Subsequent entry prechecks reuse this cache.
                    trader.start_account_snapshots()
                    trader._account_snapshot_cache.background_ready = lambda: not any(
                        p.status != 'closed' for p in self.strategy.positions)
                    snapshot = trader._account_snapshot_cache.refresh()
                    assert_new_account_flat(trader, snapshot[0])
                if callable(getattr(type(trader), 'expand_reference_feed', None)):
                    trader.expand_reference_feed(request['symbols'])
                else:
                    trader.start_reference_feed(request['symbols'])
                selected.append(trader)
            def commit():
                self.adapters.extend(created)
                self.clients.extend(resources)
                self.started_feeds.extend(created)
                self.by_venue.update({t.venue:t for t in created})
            return dict(adapters=selected, created=created, resources=resources, commit=commit)
        except Exception:
            self.dispose(dict(created=created, resources=resources))
            raise

    @staticmethod
    def dispose(prepared):
        for trader in reversed(prepared.get('created', [])):
            for method in ('stop_account_snapshots', 'close_reference_feed'):
                try:
                    getattr(trader, method)()
                except Exception:
                    pass
        for resource in reversed(prepared.get('resources', [])):
            try:
                resource.close()
            except Exception:
                pass
