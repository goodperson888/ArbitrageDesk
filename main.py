#!/usr/bin/env python3
"""Official public-data monitor; --live fails closed until adapters are verified."""
import argparse
import json
from pathlib import Path
from src.web import app as dashboard


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', default=str(Path(__file__).parent / 'config/config.yaml'))
    parser.add_argument('--once', action='store_true', help='获取一次真实行情并退出')
    parser.add_argument('--preflight', action='store_true', help='输出实盘缺失条件')
    parser.add_argument('--live', action='store_true', help='检查实盘资格（未满足时拒绝启动）')
    args = parser.parse_args()
    dashboard.init_system(args.config)
    if args.preflight or args.live:
        print(json.dumps(dashboard.strategy.get_strategy_status(), ensure_ascii=False, indent=2))
        if args.live:
            raise SystemExit('实盘适配器与成交核对尚未验收，未发送订单')
        return
    if args.once:
        print(json.dumps(dashboard.refresh_prices(), ensure_ascii=False, indent=2))
        return
    dashboard.start_monitor()
    dashboard.app.run(host='127.0.0.1', port=5001, debug=False, use_reloader=False)


if __name__ == '__main__':
    main()
