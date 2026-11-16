import copy
import hashlib
import unittest
from src.variational.omni_contract import identity
from src.variational.schema_replay import replay_receipts
from test_omni_contract import ACCOUNT, page, order, trade


class HistoricalReplayTests(unittest.TestCase):
    def setUp(self):
        digest=lambda value:hashlib.sha256(value.encode()).hexdigest()[:16]
        self.events=[];orders=[];trades=[]
        for i,(side,reduce) in enumerate((('buy',False),('sell',True))):
            q='q'+str(i);r='r'+str(i)
            self.events.extend([
                dict(kind='quote_response',quote_id_digest=digest(q),symbol='ETH',qty=.004),
                dict(kind='submit_request',quote_id_digest=digest(q),capture_id=str(i),side=side,is_reduce_only=reduce,max_slippage=.001),
                dict(kind='submit_response',capture_id=str(i),rfq_id_digest=digest(r),endpoint='/api/orders/new/market')])
            orders.append(order(order_id='o'+str(i),rfq_id=r,side=side,is_reduce_only=reduce))
            trades.append(trade(id='t'+str(i),source_rfq=r,side=side))
        self.orders=page(orders);self.trades=page(trades)

    def test_both_actual_intents_replay_without_claiming_new_fills(self):
        result=replay_receipts(identity(ACCOUNT),self.events,self.orders,self.trades)
        self.assertTrue(result['verified'])
        self.assertFalse(result['orders_submitted'])
        self.assertFalse(result['live_execution_verified'])
        self.assertEqual(len(result['receipts']),2)

    def test_wrong_account_quantity_reduce_flag_and_missing_correlation_reject(self):
        for change in ({'company':'wrong'},{'qty':'.008'},{'is_reduce_only':True},{'rfq_id':'wrong'}):
            orders=copy.deepcopy(self.orders);orders['result'][0].update(change)
            with self.subTest(change=change),self.assertRaises(ValueError):
                replay_receipts(identity(ACCOUNT),self.events,orders,self.trades)

    def test_unconfirmed_or_duplicate_trades_reject(self):
        trades=copy.deepcopy(self.trades);trades['result'][0]['status']='pending'
        with self.assertRaises(ValueError):replay_receipts(identity(ACCOUNT),self.events,self.orders,trades)
        trades=copy.deepcopy(self.trades);trades['result'].append(trades['result'][0])
        with self.assertRaises(ValueError):replay_receipts(identity(ACCOUNT),self.events,self.orders,trades)
