import yaml
import os
import copy
import math
from pathlib import Path
from typing import Dict, Any
from urllib.parse import urlsplit
from dotenv import load_dotenv


class ConfigLoader:
    """配置加载器"""
    
    def __init__(self, config_path: str = "config/config.yaml"):
        self.config_path = Path(config_path).resolve()
        self.config: Dict[str, Any] = {}
        self._load_env()
        self._load_config()
    
    def _load_env(self) -> None:
        """加载环境变量"""
        env_path = self.config_path.parent.parent / '.env'
        if env_path.exists():
            load_dotenv(env_path)
        # Public market data does not require a wallet or a .env file.
    
    def _load_config(self) -> None:
        """加载YAML配置文件"""
        if not self.config_path.exists():
            raise FileNotFoundError(f"配置文件不存在: {self.config_path}")
        
        with open(self.config_path, 'r', encoding='utf-8') as f:
            self.config = yaml.safe_load(f)
    
    def get(self, key: str, default: Any = None) -> Any:
        """获取配置项"""
        keys = key.split('.')
        value = self.config
        
        for k in keys:
            if isinstance(value, dict) and k in value:
                value = value[k]
            else:
                return default
        
        return value
    
    def get_env(self, key: str, default: Any = None) -> Any:
        """获取环境变量"""
        return os.getenv(key, default)
    
    def get_variational_config(self) -> Dict[str, Any]:
        """获取Variational配置"""
        return self.config.get('variational', {})
    
    def get_lighter_config(self) -> Dict[str, Any]:
        """获取Lighter配置"""
        return self.config.get('lighter', {})
    
    def get_strategy_config(self) -> Dict[str, Any]:
        """获取策略配置"""
        return self.config.get('strategy', {})
    
    def get_common_config(self) -> Dict[str, Any]:
        """获取通用配置"""
        return self.config.get('common', {})

    def update_strategy(self, payload):
        """Validate only supported UI settings and atomically persist them."""
        allowed = {
            'spread_threshold': {'min': (0, 10000), 'max': (0, 10000)},
            'trading': {'position_size': (1, 1000000), 'max_positions': (1, 100),
                        'max_total_position_notional': (0, 1000000),
                        'capital_reserve_percent': (0, 99), 'capital_reserve_usdc': (0, 1000000),
                        'leverage': (1, 100), 'second_leg_retries': (0, 2)},
            'wash_trading': {'min_trades_per_day': (0, 10000), 'max_trades_per_day': (1, 10000)},
            'risk': {'max_loss_per_trade': (0.01, 1000000), 'max_daily_loss': (0.01, 1000000),
                     'slippage_tolerance': (0, 100), 'execution_safety_margin_bps': (0, 50),
                     'entry_extra_profit_bps': (0, 50),
                     'execution_price_movement_bps': (0, 100),
                     'book_utilization_percent': (0.1, 100),
                     'hedge_max_delay_seconds': (.25, 30), 'route_rollback_limit': (1, 10),
                     'route_rollback_cooldown_seconds': (60, 86400)},
            'cost_model': {'variational_fee_bps': (0, 10000), 'funding_bps_per_8h': (-10000, 10000)},
            'close_policy': {
                'convergence_bps': (0, 10000),
                'cost_reserve_usdc': (0, 1000000),
                'min_profit_usdc': (0, 1000000),
                'min_profit_bps': (0, 10000),
                'max_hold_seconds': (0, 7 * 24 * 60 * 60),
            },
            'entry_policy': {
                'quantile': (50, 99.9),
                'min_samples': (30, 100000),
                'calculation_version': (2, 2),
                'target_window_seconds': (300, 7776000),
                'funding_reserve_bps': (0, 10000),
                'funding_hold_seconds': (60, 604800),
                'funding_extra_bps': (0, 10000),
            },
            'position_policy': {'step_bps': (.1, 10000), 'max_layers': (1, 100),
                                'layer_notional_usdc': (1, 1000000), 'total_notional_usdc': (1, 1000000)},
            'channel_policy': {'window_seconds': (900,7776000), 'quantile': (50.1,99.9),
                'multiplier': (.1,10), 'min_samples': (30,100000), 'min_width_bps': (0,10000),
                'max_width_bps': (0,10000), 'lower_bps': (-9999,9999), 'upper_bps': (-9999,9999),
                'reversion_percent': (.1,100), 'touch_tolerance_bps': (.1,100), 'touch_min_count': (2,50)},
        }
        close_modes = {'convergence', 'profit', 'convergence_and_profit',
                       'convergence_or_profit', 'disabled'}
        convergence_targets = {'center', 'zero', 'either'}
        entry_modes = {'fixed', 'adaptive_quantile', 'channel'}
        if not isinstance(payload, dict) or set(payload) != {'strategy'} or not isinstance(payload['strategy'], dict):
            raise ValueError('配置必须只包含 strategy 对象')
        new = copy.deepcopy(self.config)
        target = new.setdefault('strategy', {})
        for group, values in payload['strategy'].items():
            if group == 'volume_priority' and isinstance(values, bool):
                target[group] = values
                continue
            if group not in allowed or not isinstance(values, dict):
                raise ValueError('不支持的配置项: ' + group)
            for key, value in values.items():
                if group == 'trading' and key == 'capital_allocation_mode':
                    if value not in ('per_leg', 'remaining_slots'):
                        raise ValueError('资金分配模式无效')
                    target.setdefault(group, {})[key] = value
                    continue
                if group == 'channel_policy' and key in ('method','exit_mode'):
                    choices = ('quantile','bollinger','robust','fixed','touches') if key=='method' else ('reversion','opposite')
                    if value not in choices:
                        raise ValueError('通道类型或退出模式无效')
                    target.setdefault(group, {})[key] = value
                    continue
                if group == 'position_policy' and key == 'mode':
                    if value not in ('single', 'spread_grid'):
                        raise ValueError('持仓模式无效')
                    target.setdefault(group, {})[key] = value
                    continue
                if group == 'close_policy' and key == 'convergence_basis':
                    if value not in ('remaining', 'reduction'):
                        raise ValueError('收敛计算方式无效')
                    target.setdefault(group, {})[key] = value
                    continue
                if group == 'close_policy' and key == 'mode':
                    if not isinstance(value, str) or value not in close_modes:
                        raise ValueError('平仓条件模式无效')
                    target.setdefault(group, {})[key] = value
                    continue
                if group == 'close_policy' and key == 'target_mode':
                    if value not in convergence_targets:
                        raise ValueError('平仓回归目标无效')
                    target.setdefault(group, {})[key] = value
                    continue
                if group == 'entry_policy' and key == 'mode':
                    if value not in entry_modes:
                        raise ValueError('入场方式无效')
                    target.setdefault(group, {})[key] = value
                    continue
                if group == 'entry_policy' and key == 'funding_mode':
                    if value not in ('fixed', 'dynamic'):
                        raise ValueError('资金费估算模式无效')
                    target.setdefault(group, {})[key] = value
                    continue
                if group == 'trading' and key == 'leverage_mode':
                    if value not in ('verify_only', 'enforce_common'):
                        raise ValueError('杠杆模式无效')
                    target.setdefault(group, {})[key] = value
                    continue
                if group == 'risk' and key in ('max_loss_per_trade', 'max_daily_loss') and value is None:
                    target.setdefault(group, {})[key] = None
                    continue
                if group == 'cost_model' and key in ('variational_fee_bps', 'funding_bps_per_8h') and value is None:
                    target.setdefault(group, {})[key] = None
                    continue
                if key not in allowed[group] or isinstance(value, bool) or not isinstance(value, (int, float)):
                    raise ValueError('配置项格式错误: ' + key)
                low, high = allowed[group][key]
                if not math.isfinite(value) or not low <= value <= high:
                    raise ValueError('配置项超出范围: ' + key)
                if key in ('max_positions', 'min_trades_per_day', 'max_trades_per_day', 'max_hold_seconds', 'leverage', 'second_leg_retries', 'min_samples', 'route_rollback_limit', 'max_layers', 'calculation_version', 'target_window_seconds') and int(value) != value:
                    raise ValueError('次数、持仓数与最长持仓必须为整数')
                target.setdefault(group, {})[key] = value
        for group, low_key, high_key in [('spread_threshold', 'min', 'max'),
                                        ('wash_trading', 'min_trades_per_day', 'max_trades_per_day')]:
            values = target.get(group, {})
            if values.get(low_key, 0) > values.get(high_key, float('inf')):
                raise ValueError('最小值不能大于最大值')
        from src.execution.grid_policy import GridPolicy
        GridPolicy.from_config(target)
        from src.execution.channel_policy import channel_policy
        channel_policy(target)
        entry = target.get('entry_policy', {})
        if entry.get('calculation_version') == 2:
            funding = entry.get('funding_reserve_bps')
            if isinstance(funding, bool) or not isinstance(funding, (int, float)) or not math.isfinite(funding) or funding < 0:
                raise ValueError('请设置整轮资金费预算；未知成本不能按零放行')
        temporary = self.config_path.with_suffix('.tmp')
        with temporary.open('w', encoding='utf-8') as out:
            yaml.safe_dump(new, out, allow_unicode=True, sort_keys=False)
            out.flush()
            os.fsync(out.fileno())
        os.replace(temporary, self.config_path)
        self.config = new

    def update_network(self, payload):
        """Persist the shared local proxy used by public feeds and browser/API clients."""
        if not isinstance(payload, dict) or set(payload) != {'proxy_url'}:
            raise ValueError('网络配置必须只包含 proxy_url')
        value = payload['proxy_url']
        if value is None:
            value = ''
        if not isinstance(value, str):
            raise ValueError('代理地址必须是文本或留空')
        value = value.strip()
        if value:
            parsed = urlsplit(value)
            if parsed.scheme not in ('http', 'https', 'socks5', 'socks5h') or not parsed.hostname:
                raise ValueError('代理地址需使用 http://、https://、socks5:// 或 socks5h://')
            if parsed.username or parsed.password or parsed.path not in ('', '/') or parsed.query or parsed.fragment:
                raise ValueError('代理地址只填写协议、主机和端口，不要包含账号、密码或路径')
            try:
                port = parsed.port
            except ValueError:
                raise ValueError('代理端口无效') from None
            if port is not None and not 1 <= port <= 65535:
                raise ValueError('代理端口需在 1–65535 之间')
        normalized = value or None
        new = copy.deepcopy(self.config)
        new.setdefault('common', {}).setdefault('network', {})['proxy_url'] = normalized
        # The dashboard exposes one shared local proxy setting. Keep the
        # existing platform sections in sync so an old per-venue value cannot
        # silently override what the user just saved.
        for section in ('variational', 'lighter', 'lighter_rh', 'standx', 'binance', 'gate', 'okx', 'arcus'):
            target = new.setdefault(section, {})
            if section == 'variational':
                target.setdefault('api', {})['proxy_url'] = normalized
                target.setdefault('browser', {})['proxy'] = normalized
            else:
                target.setdefault('api', {})['proxy_url'] = normalized
        temporary = self.config_path.with_suffix('.tmp')
        with temporary.open('w', encoding='utf-8') as out:
            yaml.safe_dump(new, out, allow_unicode=True, sort_keys=False)
            out.flush()
            os.fsync(out.fileno())
        os.replace(temporary, self.config_path)
        self.config = new
