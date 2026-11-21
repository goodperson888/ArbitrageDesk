"""Durable submission intents: an ambiguous send is never automatically retried."""
import json
import sqlite3
from pathlib import Path
from contextlib import contextmanager, closing
from src.common import file_lock as fcntl


class OrderJournal:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self.connect()) as db, db:
            db.execute('PRAGMA journal_mode=WAL')
            db.execute('''CREATE TABLE IF NOT EXISTS orders (
                seq INTEGER PRIMARY KEY AUTOINCREMENT, venue TEXT NOT NULL,
                account TEXT NOT NULL, client_id TEXT NOT NULL, intent TEXT NOT NULL,
                state TEXT NOT NULL, result TEXT, UNIQUE(venue, account, client_id))''')

    def connect(self):
        db = sqlite3.connect(str(self.path), timeout=10)
        db.execute('PRAGMA synchronous=FULL')
        db.row_factory = sqlite3.Row
        return db

    def reserve(self, venue, account, client_id, intent):
        if not client_id or len(client_id) > 128:
            raise ValueError('需要唯一 client_order_id')
        serialized = json.dumps(intent, sort_keys=True, allow_nan=False)
        with closing(self.connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            old = db.execute('SELECT * FROM orders WHERE venue=? AND account=? AND client_id=?',
                             (venue, str(account), client_id)).fetchone()
            if old:
                if old['intent'] != serialized:
                    raise ValueError('同一订单编号不能改变参数')
                return dict(old), False
            unresolved = db.execute("SELECT seq FROM orders WHERE venue=? AND account=? AND state IN ('submitting','unknown')",
                                    (venue, str(account))).fetchone()
            # An unknown entry intent must block any new exposure, but a
            # separately identified reduce-only close is the safe recovery
            # path after a confirmed fill whose fee/accounting evidence is
            # incomplete. The adapter still validates the live position and
            # active orders before it can submit this close.
            if unresolved and not bool(intent.get('reduce_only')):
                raise RuntimeError('存在未核对订单，禁止新提交')
            cursor = db.execute('INSERT INTO orders(venue,account,client_id,intent,state) VALUES(?,?,?,?,?)',
                                (venue, str(account), client_id, serialized, 'submitting'))
            row = db.execute('SELECT * FROM orders WHERE seq=?', (cursor.lastrowid,)).fetchone()
            return dict(row), True

    def finish(self, seq, result):
        state = 'terminal' if result.get('confirmed') is True else 'unknown'
        if result.get('filled_size', 0) and result.get('fees_usdc') is None:
            state = 'unknown'
        with closing(self.connect()) as db, db:
            db.execute('UPDATE orders SET state=?,result=? WHERE seq=?',
                       (state, json.dumps(result, allow_nan=False), seq))

    def get(self, seq):
        with closing(self.connect()) as db, db:
            row = db.execute('SELECT * FROM orders WHERE seq=?', (seq,)).fetchone()
        return dict(row) if row else None

    def find(self, venue, account, client_id):
        """Read one intent by its durable identity, without reserving a send."""
        with closing(self.connect()) as db, db:
            row = db.execute('SELECT * FROM orders WHERE venue=? AND account=? AND client_id=?',
                             (venue, str(account), client_id)).fetchone()
        return dict(row) if row else None

    def summary(self):
        with closing(self.connect()) as db, db:
            return [dict(r) for r in db.execute('SELECT seq,venue,client_id,state FROM orders ORDER BY seq DESC LIMIT 100')]


@contextmanager
def execution_lease(path):
    """Hold for the worker's entire lifetime, never delete/recreate the lock file."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a') as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError('已有执行进程持有锁') from None
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)
