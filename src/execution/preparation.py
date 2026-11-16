"""Context marker for optional, read-only candidate work."""
from contextvars import ContextVar
from contextlib import contextmanager
from functools import wraps
import copy
import threading
from src.common.market_data import age_seconds

_preparing = ContextVar('entry_readonly_preparation', default=False)
_books = ContextVar('entry_preparation_books', default=None)


def is_preparing():
    return _preparing.get()


def prepared_book(trader, symbol):
    state = _books.get()
    if state is None:
        return None
    with state['lock']:
        book = state['books'].get((id(trader), symbol))
        age = age_seconds(book.get('fetched_at')) if isinstance(book, dict) else None
        return copy.deepcopy(book) if age is not None and 0 <= age <= 2 else None


def preparation_book(reader):
    """Singleflight one fresh raw book per venue/symbol in one preparation.

    copy_context shares this scope across parallel quantity quotes. Raw
    levels can serve a smaller quantity, but never a later execution phase.
    """
    @wraps(reader)
    def read(trader, symbol):
        state = _books.get()
        if state is None:
            return reader(trader, symbol)
        key = (id(trader), symbol)
        with state['lock']:
            lock = state['key_locks'].setdefault(key, threading.RLock())
        with lock:
            cached = prepared_book(trader, symbol)
            if cached is not None:
                return cached
            result = reader(trader, symbol)
            if isinstance(result, dict):
                with state['lock']:
                    state['books'][key] = copy.deepcopy(result)
            return result
    return read


@contextmanager
def readonly_preparation():
    token = _preparing.set(True)
    books_token = _books.set(dict(lock=threading.RLock(), books={}, key_locks={}))
    try:
        yield
    finally:
        _books.reset(books_token)
        _preparing.reset(token)
