"""Verify receipt parsing using authenticated history plus captured manual intents.

This is an isolated historical replay, never a current fill or trading journal.
Raw IDs and account payloads remain in memory; reports contain no credentials.
"""
import hashlib
from src.common.market_data import utc_now
from src.execution.orders import decimal
from src.variational.omni_contract import OmniContract, ORDERS, TRADES, collection
from src.variational.execution import verify_receipt


def replay_receipts(account_id, events, orders_payload, trades_payload):
    orders,_=collection(orders_payload)
    collection(trades_payload)
    digest=lambda value:hashlib.sha256(value.encode()).hexdigest()[:16] if isinstance(value,str) else None
    quotes={e.get('quote_id_digest'):e for e in events if e.get('kind')=='quote_response'}
    requests={e.get('capture_id'):e for e in events if e.get('kind')=='submit_request'}
    checked=[]
    for response in (e for e in events if e.get('kind')=='submit_response'):
        request=requests.get(response.get('capture_id'))
        quote=quotes.get(request.get('quote_id_digest')) if request else None
        if not quote or not response.get('rfq_id_digest'):
            raise ValueError('历史提交与报价关联不完整')
        matches=[o for o in orders if digest(o.get('rfq_id'))==response['rfq_id_digest']]
        if len(matches)!=1:
            raise ValueError('无法在当前账户历史找到唯一录制订单')
        order=matches[0]
        side=request['side'];reduce_only=request['is_reduce_only']
        if type(reduce_only) is not bool:
            raise ValueError('历史只减仓字段未知')
        # The original raw quote ID was deliberately redacted at capture time.
        # Its fingerprint is used consistently in this isolated parser replay.
        bound=dict(account_id=account_id,quote_id=quote['quote_id_digest'],symbol=quote['symbol'],
                   size=str(quote['qty']),max_slippage_bps=decimal(request['max_slippage'])*10000)
        contract=OmniContract(account_id)
        at=utc_now()
        contract.arm(bound,side,bound['size'],reduce_only,at)
        contract.ingest(response['endpoint'],dict(quote_id=bound['quote_id'],side=side,
            is_reduce_only=reduce_only,max_slippage=request['max_slippage']),{'rfq_id':order['rfq_id']},at)
        contract.ingest(ORDERS,{},orders_payload,at)
        contract.ingest(TRADES,{},trades_payload,at)
        receipt=verify_receipt(contract.receipt,bound,side,bound['size'],reduce_only)
        checked.append({'side':side,'reduce_only':reduce_only,'filled_size':receipt['filled_size'],
                        'average_price':receipt['average_price'],'status':receipt['status']})
    if len(checked)!=2 or {(r['side'],r['reduce_only']) for r in checked}!={('buy',False),('sell',True)}:
        raise ValueError('缺少真实开仓与只减仓平仓的回执覆盖')
    return {'verified':True,'kind':'historical_parser_replay','live_execution_verified':False,
            'orders_submitted':False,'verified_at':utc_now(),'receipts':checked}
