import json
import os
import tempfile
import unittest
from pathlib import Path
from src.execution.reset_stop import reset_stop, reconcile_unknown_orders
from src.execution.journal import OrderJournal, execution_lease

class ResetTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);(self.root/'data').mkdir()
        self.stop=self.root/'data/STOP_EXECUTION';self.stop.touch()
    def test_clears_only_stop_after_account_check(self):
        state=self.root/'data/hedge_state.json';state.write_text(json.dumps({'positions':[],'halted_reason':None}))
        before=state.read_bytes()
        reset_stop(self.root,lambda:True)
        self.assertFalse(self.stop.exists());self.assertEqual(before,state.read_bytes())
    def test_account_failure_preserves_stop(self):
        with self.assertRaises(ValueError):reset_stop(self.root,lambda:False)
        self.assertTrue(self.stop.exists())
    def test_unresolved_orders_preserve_stop(self):
        OrderJournal(self.root/'data/execution_orders.sqlite').reserve('v','a','id',{})
        with self.assertRaises(ValueError):reset_stop(self.root,lambda:True)
        self.assertTrue(self.stop.exists())

    def test_flat_account_reconciles_unknown_fee_receipt_before_reset(self):
        state=self.root/'data/hedge_state.json';state.write_text(json.dumps({'positions':[],'halted_reason':None}))
        journal=OrderJournal(self.root/'data/execution_orders.sqlite')
        journal.reserve('lighter','a','id',{'symbol':'OPENAI'})
        journal.finish(1,{'confirmed':True,'filled_size':1,'fees_usdc':None})
        reset_stop(self.root, lambda: True,
                   lambda: reconcile_unknown_orders(self.root, ['lighter','lighter_rh']))
        self.assertFalse(self.stop.exists())
        self.assertEqual(journal.get(1)['state'], 'terminal')
        self.assertTrue(json.loads(journal.get(1)['result'])['reconciled'])
    def test_open_or_halted_local_state_preserves_stop(self):
        for state in ({'positions':[{'status':'open'}]}, {'positions':[],'halted_reason':'unknown'}):
            (self.root/'data/hedge_state.json').write_text(json.dumps(state))
            with self.assertRaises(ValueError):reset_stop(self.root,lambda:True)
            self.assertTrue(self.stop.exists())
    def test_new_stop_during_check_is_preserved(self):
        def verify():
            stamp=self.stop.stat();os.utime(self.stop,ns=(stamp.st_atime_ns,stamp.st_mtime_ns+1));return True
        with self.assertRaises(ValueError):reset_stop(self.root,verify)
        self.assertTrue(self.stop.exists())
    def test_worker_lock_prevents_reset(self):
        with execution_lease(self.root/'data/runtime.lock'):
            with self.assertRaises(RuntimeError):reset_stop(self.root,lambda:True)
        self.assertTrue(self.stop.exists())
