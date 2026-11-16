# Arbitrage Desk

本机运行的多平台价差工作台：双向机会扫描、历史与通道策略、等量双腿执行、持仓监控和平仓核账。已接入 Lighter 主网、Lighter RH、Arcus、StandX；Variational 提供行情与独立浏览器路径。

## Windows

在本仓库的 **Actions → Windows portable package** 下载最近成功构建的 `ArbitrageDesk-windows-x64`。解压后双击 `start-windows.cmd`，浏览器打开 `http://127.0.0.1:5001`；双击 `stop-windows.cmd` 结束服务。

压缩包自带 Python 和依赖，目标为 Windows 10/11 x64。构建及首次启动不会自动开启实盘。详情见 [Windows 使用与构建说明](docs/windows-package.md)。

## macOS / 源码运行

```bash
./start.sh
./start.sh status
./start.sh stop
```

默认仅监听本机：面板 `127.0.0.1:5001`，只读 API `127.0.0.1:5002`。首次启动准备环境，后台运行后关闭终端仍会继续服务。代理、平台连接、金额与策略在页面设置中配置。

启动服务后，选择平台和品种，完成只读检查，再手动开启实盘。停止新开仓后继续管理已有持仓；停止服务不代表已平仓。成交未知时保留记录并暂停新开仓，需核对账户；不要删除状态文件后重试。

密钥、交易历史、浏览器会话和实际配置仅保存在本机，不随源码或 Windows 包分发。新机器必须重新配置并核验账户。

## 开发检查

```bash
.venv/bin/python -B scripts/run_tests.py --scope web
.venv/bin/python -B scripts/run_tests.py --scope strategy
.venv/bin/python -B scripts/run_tests.py --scope full
```

按改动范围运行局部测试；涉及成交、资金、平仓、恢复或跨模块变更后再做一次完整回归。测试不访问真实账户或发送订单。详见 [测试说明](docs/testing.md)。

## 构建 Windows 包

推送 `main`、`v*` 标签，或手动启动 Actions。Windows 构建机下载官方嵌入式 Python、安装依赖并执行离线检查，通过后上传便携 ZIP。构建不需要交易密钥。
