"""Entry-only limits that can change between hedge submissions."""
import copy
import math
from src.common.sizing import book_utilization_percent


ENTRY_LIMIT_KEYS = ('position_size', 'max_positions', 'max_total_position_notional')


def validated_entry_limits(config):
    trading = config.get('trading', {})
    result = {}
    for key, default, low, high in (
            ('position_size', 100, 1, 1000000),
            ('max_positions', 1, 1, 100),
            ('max_total_position_notional', 0, 0, 1000000)):
        value = trading.get(key, default)
        if (isinstance(value, bool) or not isinstance(value, (int, float)) or
                not math.isfinite(value) or not low <= value <= high or
                (key == 'max_positions' and int(value) != value)):
            raise ValueError('新开仓限制无效：' + key)
        result[key] = int(value) if key == 'max_positions' else float(value)
    return result


class EntryLimitsMixin:
    def _apply_entry_limits(self, loaded):
        limits = validated_entry_limits(loaded)
        utilization = book_utilization_percent(loaded.get('risk', {}).get('book_utilization_percent', 80))
        # Legacy launches and explicit temporary caps keep their run ceiling.
        cap = self.config.get('execution_notional_cap')
        self.position_size = min(limits['position_size'], cap) if cap is not None else limits['position_size']
        self.max_positions = limits['max_positions']
        self.max_total_position_notional = limits['max_total_position_notional']
        self.config.setdefault('trading', {}).update(limits)
        self.book_utilization_percent = utilization
        self.config.setdefault('risk', {})['book_utilization_percent'] = utilization

    def _reload_open_entry_limits(self, loaded, mtime):
        if mtime == getattr(self, '_entry_limits_mtime_ns', self._config_mtime_ns):
            return False
        before = {key: self.config.get('trading', {}).get(key, default) for key, default in
                  zip(ENTRY_LIMIT_KEYS, (100, 1, 0))}
        previous_utilization = self.book_utilization_percent
        # Validate all three before touching any live value. Positions, exit
        # policy, grid parameters and venue/session objects remain untouched.
        self._apply_entry_limits(loaded)
        self._entry_limits_mtime_ns = mtime
        self._pending_runtime_settings = self._other_settings_differ(loaded)
        changed = (before != {key: self.config['trading'][key] for key in ENTRY_LIMIT_KEYS} or
                   previous_utilization != self.book_utilization_percent)
        return changed

    def _other_settings_differ(self, loaded):
        def policy(config):
            value = copy.deepcopy(config)
            for key in ('mode', 'state_path', 'config_path', 'execution_filters',
                        'funding_accounting_mode', 'execution_notional_cap'):
                value.pop(key, None)
            trading = value.get('trading', {})
            for key in ENTRY_LIMIT_KEYS:
                trading.pop(key, None)
            if not trading:
                value.pop('trading', None)
            risk = value.get('risk', {})
            risk.pop('book_utilization_percent', None)
            if not risk:
                value.pop('risk', None)
            return value
        return policy(loaded) != policy(self.config)

    def entry_limits_status(self):
        with self.lock:
            cap = self.config.get('execution_notional_cap')
            saved_cap = self.config.get('trading', {}).get('position_size', self.position_size)
            limits = {'每腿设置': saved_cap, '本次累计目标剩余额度': self.position_size}
            if cap is not None:
                limits['本次临时上限'] = cap
            remaining = self.remaining_open_notional()
            if remaining is not None:
                limits['总持仓剩余额度'] = remaining
            limits['网格额度'] = self._grid_cap()
            effective = max(0, min(limits.values()))
            sources = [name for name, value in limits.items() if value == effective and
                       (name != '本次累计目标剩余额度' or value < min(saved_cap, cap if cap is not None else saved_cap))]
            return dict(version=1, max_positions=self.max_positions,
                        max_total_position_notional=self.max_total_position_notional,
                        position_size=self.position_size,
                        saved_position_size=self.config.get('trading', {}).get('position_size', self.position_size),
                        execution_notional_cap=self.config.get('execution_notional_cap'),
                        cap_mode='temporary' if cap is not None else 'follow_saved',
                        effective_entry_cap=effective, limiting_sources=sources,
                        active_groups=sum(p.status != 'closed' for p in self.positions),
                        single_position_cycle=getattr(self, 'runtime_single_position_cycle', False),
                        pending_other_settings=getattr(self, '_pending_runtime_settings', False))
