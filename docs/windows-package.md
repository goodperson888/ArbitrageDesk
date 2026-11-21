# Windows 包与 GitHub 构建

仓库：`https://github.com/goodperson888/ArbitrageDesk`

## 下载与使用

1. GitHub → Actions → Windows portable package → 最近成功的运行。
2. 下载 `ArbitrageDesk-windows-x64`，解压下载物和里面的程序压缩包。
3. 把整个 `ArbitrageDesk` 文件夹放到当前用户可写的目录，双击 `start-windows.cmd`。
4. 打开 `http://127.0.0.1:5001`，在设置中配置平台连接、代理及交易策略。
5. 停止所有服务：双击 `stop-windows.cmd`。停止服务不代表平仓。

包内带 Python 和依赖，不需要另装 Python。当前目标为 Windows 10/11 x64，使用浏览器界面；这是便携目录包，不是单文件 exe 安装程序。不要单独移动 python.exe，也不要放在公共共享目录中保存密钥。

首次运行从示例生成本机配置。实盘默认关闭；启动不自动开仓、不解除 STOP，不迁移 Mac 的密钥、持仓或历史。旧包升级时先停止服务，备份自己的 `.env`、`config/config.yaml`、本机执行配置和 `data/`，确认账户持仓后再迁移。不要同时运行新旧目录。

通用签名平台包含 Lighter、Lighter RH、Arcus、StandX。CI 仅离线验证导入、页面模板、原生签名 DLL、文件锁和进程生命周期；真实账户与成交仍需用户在 Windows 本机检查。Variational 的可选浏览器路径还需要本机 Chrome 和匹配的 ChromeDriver，包中不附带浏览器、钱包或驱动。

## 构建

在 GitHub 的 Actions 中选择 `Windows portable package`，点击 `Run workflow` 手动打包；推送代码或标签不会自动打包。构建机为 Windows x64，安装依赖、检查干净包后上传 ZIP，保留 14 天。失败不上传可用包；从对应步骤日志查看原因。

`scripts/build_windows.py` 通过明确目录清单组装，排除运行目录；仅携带示例配置。Windows 首次启动由 `scripts/windows_launcher.py` 管理面板、API 及它们的子进程。现有 Mac 启停命令继续使用 `start.sh`。

Windows 密钥文件使用当前用户目录继承的 ACL，POSIX 系统继续使用 0600。不要把实际 `.env`、本机配置、数据库、诊断导出或浏览器会话提交到仓库。构建不需要交易密钥，也不应配置这类 Actions Secret。
