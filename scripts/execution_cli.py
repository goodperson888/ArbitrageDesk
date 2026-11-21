#!/usr/bin/env python3
"""Execution status by default; --run requires a locally verified account profile."""
import argparse
import json
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from src.execution.status import implementation_status
from src.execution.runtime import runtime_blockers, run_runtime
from src.common.config_loader import ConfigLoader


if __name__ == '__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--profile', default=str(ROOT/'config/variational-execution.example.json'))
    parser.add_argument('--config', default=str(ROOT/'config/config.yaml'))
    parser.add_argument('--run', action='store_true')
    parser.add_argument('--arm-once', action='store_true', help='仅为本次单次或等待单次执行临时启用，不修改默认关闭配置')
    parser.add_argument('--once', action='store_true', help='只尝试一次双腿入场，不循环、不处理旧仓位')
    parser.add_argument('--wait-once', action='store_true', help='持续等待下一次合格机会，只提交一次双腿入场')
    parser.add_argument('--symbol', help='兼容旧版的首个执行市场')
    parser.add_argument('--symbols', help='执行市场列表，逗号分隔；全品种模式由扫描器提供')
    parser.add_argument('--max-notional', type=float, help='每腿名义金额上限，不能高于已保存配置')
    parser.add_argument('--follow-saved-cap', action='store_true',
                        help='后续新仓跟随已保存的每腿上限；默认保留本次固定上限')
    parser.add_argument('--target-notional', type=float, default=0,
                        help='本次累计单腿开仓目标，取每组成对成交较小腿，不含平仓、不乘杠杆；0 为不限')
    parser.add_argument('--no-candidate-fallback', action='store_true',
                        help='候选未提交时不自动切换下一条机会')
    parser.add_argument('--allow-multiple-positions', action='store_true',
                        help='允许按策略并发上限寻找下一条机会；默认单仓平仓后再开')
    parser.add_argument('--close-only', action='store_true',
                        help='只管理并平掉已有仓位，不再寻找或提交新开仓')
    parser.add_argument('--gross-spread-min-bps', type=float, default=0,
                        help='毛价差绝对值下限（bps），0 表示不额外限制')
    parser.add_argument('--gross-spread-max-bps', type=float, default=0,
                        help='毛价差绝对值上限（bps），0 表示不额外限制')
    parser.add_argument('--reversion-target-mode', choices=('center', 'zero', 'either'), default='center',
                        help='历史回归目标：历史中心、0 或任一目标')
    parser.add_argument('--reversion-window-seconds', type=int, default=604800,
                        help='历史回归统计窗口，单位秒，范围 300 至 7776000（90天）')
    parser.add_argument('--reversion-min-count', type=int, default=0,
                        help='窗口内最少回归次数，0 表示不限')
    parser.add_argument('--reversion-min-deviation-bps', type=float, default=0,
                        help='计入回归前要求的最小偏离，单位 bps')
    parser.add_argument('--reversion-max-age-seconds', type=float, default=0,
                        help='距上次回归最大秒数，0 表示不限')
    parser.add_argument('--reversion-recency-mode', choices=('hard', 'soft'), default='hard',
                        help='回归时效：hard 超时排除；soft 同等收益下近期回归优先')
    parser.add_argument('--reversion-min-samples', type=int, default=0,
                        help='历史最少样本数，0 表示不限')
    parser.add_argument('--venues', help='通用签名双平台，格式 venue_a,venue_b；Variational 仍走原浏览器路径')
    args=parser.parse_args()
    if args.once and args.wait_once:
        parser.error('--once 与 --wait-once 不能同时使用')
    if args.arm_once and not (args.run and (args.once or args.wait_once)):
        parser.error('--arm-once 必须与 --run --once 或 --run --wait-once 同时使用')
    logging.basicConfig(level=logging.WARNING)
    ConfigLoader(args.config)
    profile=json.loads(Path(args.profile).read_text())
    selected_venues = [item.strip() for item in args.venues.split(',') if item.strip()] if args.venues else None
    selected_symbols = [item.strip().upper().replace('-PERP', '') for item in args.symbols.split(',') if item.strip()] if args.symbols else None
    if selected_venues:
        profile['venues'] = selected_venues
        # A pair is never enabled in the saved profile.  Supplying an
        # explicit pair to the --run command is the live gate after the web
        # console has completed its read-only check.
        if args.run:
            profile['live_enabled'] = True
    if selected_symbols:
        profile['symbols'] = list(dict.fromkeys(selected_symbols))
        profile['symbol'] = profile['symbols'][0]
    if not args.run:
        print(json.dumps({**implementation_status(), 'runtime_blockers':runtime_blockers(profile)},ensure_ascii=False,indent=2))
    else:
        from src.common.quote_clock import quote_clock
        quote_clock.start()
        # The runtime rechecks saved positions against the actual accounts
        # under its execution lease before consuming a recovery proof. Let
        # that check handle an existing hedge instead of rejecting it here.
        blockers=runtime_blockers(profile, require_enabled=not args.arm_once,
                                  recover_at_start=True)
        if blockers:
            raise SystemExit('尚未启动执行：'+'；'.join(blockers))
        try:
            print(json.dumps(run_runtime(args.config,args.profile,once=args.once,wait_once=args.wait_once,
                max_notional=args.max_notional,expected_symbol=args.symbol,arm_once=args.arm_once,
                follow_saved_cap=args.follow_saved_cap,
                venues=selected_venues, symbols=selected_symbols,
                target_notional=args.target_notional,
                candidate_fallback=not args.no_candidate_fallback,
                single_position_cycle=not args.allow_multiple_positions,
                gross_spread_min_bps=args.gross_spread_min_bps,
                gross_spread_max_bps=args.gross_spread_max_bps,
                reversion_target_mode=args.reversion_target_mode,
                reversion_window_seconds=args.reversion_window_seconds,
                reversion_min_count=args.reversion_min_count,
                reversion_min_deviation_bps=args.reversion_min_deviation_bps,
                reversion_recency_mode=args.reversion_recency_mode,
                reversion_max_age_seconds=args.reversion_max_age_seconds,
                reversion_min_samples=args.reversion_min_samples,
                close_only=args.close_only),ensure_ascii=False,default=str))
        except KeyboardInterrupt:
            print('执行进程已停止；这不表示已平仓，请核对账户。')
        except Exception as error:
            from src.web.diagnostics import Diagnostics
            Diagnostics(ROOT/'data/diagnostics.sqlite').record('EXECUTION_FAILED', exc=error)
            # SDK errors can contain auth/request data; do not dump raw exceptions.
            raise SystemExit('执行已停止，需核对账户与持久状态。错误类型：'+type(error).__name__)
