"""Immutable per-position venue scopes, independent of discovery's route."""
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps


_route = ContextVar('position_venue_route', default=None)


def for_position(method):
    @wraps(method)
    def call(self, position, *args, **kwargs):
        with self.position_route(position):
            return method(self, position, *args, **kwargs)
    return call


class PositionRoutesMixin:
    def _scoped_trader(self, index):
        scope = _route.get()
        if scope is not None and scope[0] is self:
            return scope[1][index]
        return self._first_trader if index == 0 else self._second_trader

    @property
    def variational_trader(self):
        return self._scoped_trader(0)

    @variational_trader.setter
    def variational_trader(self, trader):
        self._first_trader = trader

    @property
    def lighter_trader(self):
        return self._scoped_trader(1)

    @lighter_trader.setter
    def lighter_trader(self, trader):
        self._second_trader = trader

    def position_traders(self, position):
        names = (position.variational_venue, position.lighter_venue)
        pairs = self._route_trader_pairs or [(self._first_trader, self._second_trader)]
        if names == (None, None):
            if self.multi_route_enabled:
                raise ValueError('持仓平台组合缺失，必须先核对原仓位')
            return tuple(pairs[0])
        if not all(names) or names[0] == names[1]:
            raise ValueError('持仓平台组合未匹配，必须先核对原仓位')
        for pair in pairs:
            by_venue = {self._venue_name(trader, ''): trader for trader in pair}
            if len(by_venue) == 2 and set(names) == set(by_venue):
                return tuple(by_venue[name] for name in names)
        raise ValueError('持仓平台组合未匹配，必须先核对原仓位')

    @contextmanager
    def position_route(self, position):
        token = _route.set((self, self.position_traders(position)))
        try:
            yield
        finally:
            _route.reset(token)
