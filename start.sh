#!/usr/bin/env bash
set -euo pipefail

# One entry point for the local dashboard, read-only API, and full shutdown.
# Start never enables live trading or submits orders. Stop requests a full
# shutdown: it stops the execution child first, then closes the dashboard/API.
# It also recognizes matching project services already listening on 5001/5002
# when a previous manual launch did not leave a PID file.
ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
cd "$ROOT_DIR"

PYTHON="$ROOT_DIR/.venv/bin/python"
RUN_DIR="$ROOT_DIR/.run"
LOG_DIR="$ROOT_DIR/logs"
WEB_PID_FILE="$RUN_DIR/web.pid"
API_PID_FILE="$RUN_DIR/api-v2.pid"
WEB_LOG="$LOG_DIR/web.log"
API_LOG="$LOG_DIR/api-v2.log"
EXEC_STOP_MARKER="$ROOT_DIR/data/STOP_EXECUTION"

mkdir -p "$RUN_DIR" "$LOG_DIR" "$ROOT_DIR/data"

say() { printf '[airdrop_arbitrage] %s\n' "$*"; }

# macOS ps/lsof escape non-ASCII paths under the C locale. Read process
# identity in an installed UTF-8 locale without changing launched apps' locale.
process_locale() {
    local available candidate
    available="$(locale -a 2>/dev/null || true)"
    for candidate in en_US.UTF-8 C.UTF-8 C.utf8 en_US.utf8; do
        case $'\n'"$available"$'\n' in
            *$'\n'"$candidate"$'\n'*) printf '%s' "$candidate"; return 0 ;;
        esac
    done
    printf '%s' "${LC_ALL:-${LC_CTYPE:-${LANG:-C}}}"
}
PROCESS_LOCALE="$(process_locale)"

ensure_runtime() {
    if [ ! -x "$PYTHON" ] || ! "$PYTHON" -B -c 'import flask, requests, yaml, dotenv, websocket' >/dev/null 2>&1; then
        say "正在准备独立运行环境…"
        bash "$ROOT_DIR/setup.sh"
    fi
}

read_pid() {
    local file="$1" pid=""
    if [ -s "$file" ]; then
        IFS= read -r pid < "$file" || true
    fi
    case "$pid" in
        ''|*[!0-9]*) printf ''; return 0 ;;
        *) printf '%s' "$pid" ;;
    esac
}

pid_alive() {
    local pid="$1" state
    [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null || return 1
    state="$(ps -p "$pid" -o stat= 2>/dev/null)" || return 1
    case "$state" in ''|*Z*) return 1 ;; esac
}

port_open() {
    local port="$1"
    (exec 3<>"/dev/tcp/127.0.0.1/${port}") >/dev/null 2>&1
}

lsof_path() {
    local resolved
    resolved="$(command -v lsof 2>/dev/null || true)"
    if [ -n "$resolved" ]; then printf '%s' "$resolved"; return 0; fi
    # GUI terminals may omit /usr/sbin from PATH on macOS.
    for resolved in /usr/sbin/lsof /usr/bin/lsof /sbin/lsof; do
        if [ -x "$resolved" ]; then printf '%s' "$resolved"; return 0; fi
    done
    return 1
}

listener_pid() {
    local port="$1" lsof_bin
    lsof_bin="$(lsof_path)" || return 0
    "$lsof_bin" -nP -tiTCP:"${port}" -sTCP:LISTEN 2>/dev/null | head -n 1 | tr -d ' ' || true
}

process_command() {
    local pid="$1"
    LC_ALL="$PROCESS_LOCALE" ps -ww -p "$pid" -o command= 2>/dev/null || true
}

process_cwd() {
    local lsof_bin
    lsof_bin="$(lsof_path)" || return 0
    LC_ALL="$PROCESS_LOCALE" "$lsof_bin" -a -p "$1" -d cwd -Fn 2>/dev/null | sed -n 's/^n//p' | head -n 1 || true
}

matches_service() {
    local name="$1" pid="$2" command executable rest
    command="$(process_command "$pid")"
    read -r executable rest <<< "$command"
    case "${executable##*/}" in python|Python|python[0-9]*|uvicorn) ;; *) return 1 ;; esac
    case "$name" in
        "行情面板")
            case "$command" in
                *"$ROOT_DIR/src/web/app.py"*) return 0 ;;
                *"src/web/app.py"*) [ "$(process_cwd "$pid")" = "$ROOT_DIR" ] && return 0 ;;
            esac
            ;;
        "统一只读 API")
            case "$command" in
                *"src.api_v2.app:app"*"--port 5002"*) [ "$(process_cwd "$pid")" = "$ROOT_DIR" ] && return 0 ;;
            esac
            ;;
    esac
    return 1
}

service_pids() {
    local name="$1" pid command
    while read -r pid command; do
        case "$name:$command" in
            "行情面板:"*"src/web/app.py"*|"统一只读 API:"*"src.api_v2.app:app"*)
                if pid_alive "$pid" && matches_service "$name" "$pid"; then
                    printf '%s\n' "$pid"
                fi ;;
        esac
    done < <(LC_ALL="$PROCESS_LOCALE" ps -ww -axo pid=,command= 2>/dev/null || true)
}

matches_execution_command() {
    local pid="$1" command="$2" executable script rest
    read -r executable script rest <<< "$command"
    case "${executable##*/}" in
        python|Python|python[0-9]*) ;;
        *) return 1 ;;
    esac
    # The launcher invokes Python directly, optionally with interpreter flags.
    # Only match the script argument, never a shell command or another script's
    # arguments containing the same filename.
    while :; do
        case "$script" in
            -B|-u|-E|-s|-S|-I|-O|-OO) read -r script rest <<< "$rest" ;;
            *) break ;;
        esac
    done
    case "$script" in
        "$ROOT_DIR/scripts/execution_cli.py") return 0 ;;
        scripts/execution_cli.py|./scripts/execution_cli.py)
            [ "$(process_cwd "$pid")" = "$ROOT_DIR" ] && return 0 ;;
    esac
    return 1
}

execution_pids() {
    local pid command
    while read -r pid command; do
        if matches_execution_command "$pid" "$command"; then
            printf '%s\n' "$pid"
        fi
    done < <(LC_ALL="$PROCESS_LOCALE" ps -ww -axo pid=,command= 2>/dev/null || true)
}

api_healthy() {
    "$PYTHON" -B - "$@" <<'PY'
import json
import sys
import urllib.request

try:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open("http://127.0.0.1:5002/api/v2/health", timeout=1.5) as response:
        data = json.load(response)
    raise SystemExit(0 if data.get("mode") == "readonly" else 1)
except Exception:
    raise SystemExit(1)
PY
}

web_healthy() {
    "$PYTHON" -B - "$@" <<'PY'
import json
import urllib.request

try:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open("http://127.0.0.1:5001/api/health", timeout=1.5) as response:
        data = json.load(response)
    raise SystemExit(0 if data.get("service") == "hedge-monitor" else 1)
except Exception:
    raise SystemExit(1)
PY
}

wait_for() {
    local name="$1" check="$2" i
    for i in $(seq 1 30); do
        if "$check"; then
            say "$name 已就绪"
            return 0
        fi
        sleep 0.5
    done
    say "$name 启动后仍未通过健康检查"
    return 1
}

start_api() {
    local pid
    pid="$(read_pid "$API_PID_FILE")"
    if pid_alive "$pid"; then
        if api_healthy; then
            say "统一只读 API 已在后台运行，健康检查通过（PID ${pid}）"
            return 0
        fi
        if ! port_open 5002; then
            : > "$API_PID_FILE"
            pid=""
        else
            say "记录的 API 进程存在，但 5002 健康检查失败；请运行 ./start.sh logs 查看日志。"
            return 1
        fi
    fi
    if api_healthy; then
        say "统一只读 API 已由其他进程运行（http://127.0.0.1:5002）"
        return 0
    fi
    say "启动统一只读 API（5002）"
    nohup "$PYTHON" -m uvicorn src.api_v2.app:app --host 127.0.0.1 --port 5002 \
        >"$API_LOG" 2>&1 < /dev/null &
    printf '%s\n' "$!" > "$API_PID_FILE"
    wait_for "统一只读 API" api_healthy || {
        say "API 日志：$API_LOG"
        return 1
    }
}

start_web() {
    local pid
    pid="$(read_pid "$WEB_PID_FILE")"
    if pid_alive "$pid"; then
        if web_healthy; then
            say "行情面板已在后台运行，健康检查通过（PID ${pid}）"
            return 0
        fi
        if ! port_open 5001; then
            : > "$WEB_PID_FILE"
            pid=""
        else
            say "记录的面板进程存在，但 5001 健康检查失败；请运行 ./start.sh logs 查看日志。"
            return 1
        fi
    fi
    if web_healthy; then
        say "行情面板已由其他进程运行（http://127.0.0.1:5001）"
        return 0
    fi
    say "启动行情面板（5001）"
    nohup "$PYTHON" -B "$ROOT_DIR/src/web/app.py" \
        >"$WEB_LOG" 2>&1 < /dev/null &
    printf '%s\n' "$!" > "$WEB_PID_FILE"
    wait_for "行情面板" web_healthy || {
        say "面板日志：$WEB_LOG"
        return 1
    }
}

stop_service_pid() {
    local name="$1" pid="$2" i
    # Recheck identity immediately before signalling, including reused PIDs.
    if ! pid_alive "$pid"; then return 0; fi
    if ! matches_service "$name" "$pid"; then
        say "$name 的进程身份已变化，未发送信号（PID ${pid}）"
        return 1
    fi
    say "停止${name}（PID ${pid}）"
    kill "$pid" 2>/dev/null || true
    for i in $(seq 1 20); do
        if ! pid_alive "$pid"; then
            say "${name} 已停止"
            return 0
        fi
        sleep 0.25
    done
    # These two services are read-only. Recheck identity before escalating;
    # never apply this fallback to the signed execution worker.
    if matches_service "$name" "$pid"; then
        say "$name 未在 5 秒内退出，清理已确认的只读服务（PID ${pid}）"
        kill -KILL "$pid" 2>/dev/null || true
        for i in $(seq 1 12); do
            if ! pid_alive "$pid"; then
                say "${name} 已停止"
                return 0
            fi
            sleep 0.25
        done
    fi
    say "$name 未确认退出（PID ${pid}），保留记录供核对"
    return 1
}

stop_one() {
    local name="$1" file="$2" port="$3" pid pids fallback seen=' ' failed=0 remaining
    pids="$(read_pid "$file")"
    fallback="$(listener_pid "$port")"
    pids="$pids $fallback $(service_pids "$name")"
    for pid in $pids; do
        case "$seen" in *" $pid "*) continue ;; esac
        seen="$seen$pid "
        if pid_alive "$pid" && matches_service "$name" "$pid"; then
            stop_service_pid "$name" "$pid" || failed=1
        fi
    done
    remaining="$(service_pids "$name")"
    if [ -n "$remaining" ]; then
        say "$name 仍有本项目进程：${remaining}；未确认停止"
        return 1
    fi
    if port_open "$port"; then
        say "端口 ${port} 仍在监听；未确认 ${name} 已停止，不会结束身份未知的进程"
        return 1
    fi
    : > "$file"
    say "${name} 进程与端口 ${port} 已确认停止"
    return "$failed"
}

stop_execution() {
    local pids pid i
    # Keep the safety marker even if the child is already gone.  A later
    # execution start must explicitly reconcile/reset the account state.
    mkdir -p "$(dirname "$EXEC_STOP_MARKER")"
    : > "$EXEC_STOP_MARKER"
    pids="$(execution_pids)"
    if [ -z "$pids" ]; then
        say "实盘执行未运行；已保留停止标志，重新执行前需先核对账户"
        return 0
    fi
    for pid in $pids; do
        if pid_alive "$pid" && matches_execution_command "$pid" "$(process_command "$pid")"; then
            say "停止实盘执行进程（PID ${pid}）"
            kill "$pid" 2>/dev/null || true
        fi
    done
    for i in $(seq 1 20); do
        if [ -z "$(execution_pids)" ]; then
            say "实盘执行已停止；已有仓位不会继续自动平仓"
            return 0
        fi
        sleep 0.25
    done
    say "实盘执行未在 5 秒内退出；保留行情面板和 API 以便核对，未继续全停"
    return 1
}

start_all() {
    ensure_runtime
    start_api
    start_web
    say "行情面板：http://127.0.0.1:5001"
    say "统一只读 API：http://127.0.0.1:5002/docs"
    say "服务在后台运行，返回终端提示符是正常现象；请在浏览器打开上方面板地址。"
    say "当前仅启动行情与只读服务；实盘仍需在页面中明确开启。"
}

stop_all() {
    stop_execution || return 1
    local failed=0
    stop_one "行情面板" "$WEB_PID_FILE" 5001 || failed=1
    stop_one "统一只读 API" "$API_PID_FILE" 5002 || failed=1
    if [ -n "$(execution_pids)" ]; then
        say "实盘执行进程仍存在，未完成全停"; failed=1
    fi
    if [ -n "$(service_pids "行情面板")$(service_pids "统一只读 API")" ]; then
        say "面板或 API 进程仍存在，未完成全停"; failed=1
    fi
    local port
    for port in 5001 5002; do
        if port_open "$port"; then say "端口 ${port} 仍在监听，未完成全停"; failed=1; fi
    done
    if [ "$failed" -eq 0 ]; then say "全停已确认：无实盘/面板/API 进程，5001/5002 已释放"; fi
    return "$failed"
}

status() {
    ensure_runtime
    local web_pid api_pid execution_count
    web_pid="$(read_pid "$WEB_PID_FILE")"
    api_pid="$(read_pid "$API_PID_FILE")"
    if web_healthy; then say "行情面板：正常（http://127.0.0.1:5001，PID ${web_pid:-外部进程}）"; else say "行情面板：未就绪"; fi
    if api_healthy; then say "统一只读 API：正常（http://127.0.0.1:5002/docs，PID ${api_pid:-外部进程}）"; else say "统一只读 API：未就绪"; fi
    execution_count="$(execution_pids | awk 'NF {count++} END {print count+0}')"
    if [ "$execution_count" -gt 0 ]; then
        say "实盘执行：运行中（${execution_count} 个进程）；停止命令会先终止它"
    elif [ -e "$EXEC_STOP_MARKER" ]; then
        say "实盘执行：未运行；停止标志存在，恢复前需账户核对"
    else
        say "实盘执行：未运行"
    fi
}

logs() {
    say "最近的面板日志：$WEB_LOG"
    tail -n 40 "$WEB_LOG" 2>/dev/null || true
    say "最近的 API 日志：$API_LOG"
    tail -n 40 "$API_LOG" 2>/dev/null || true
}

diagnose_stop() {
    say "停止识别诊断（只读，不发送信号）：Bash ${BASH_VERSION}"
    printf 'root=%q\n' "$ROOT_DIR"
    printf 'ps=%s lsof=%s\n' "$(command -v ps)" "$(lsof_path || true)"
    printf 'process_locale=%s\n' "$PROCESS_LOCALE"
    local name file port pid recorded listener
    for port in 5001 5002; do
        if [ "$port" = 5001 ]; then name='行情面板'; file="$WEB_PID_FILE";
        else name='统一只读 API'; file="$API_PID_FILE"; fi
        recorded="$(read_pid "$file")"; listener="$(listener_pid "$port")"
        say "${name}: recorded=${recorded:-none} listener=${listener:-none}"
        for pid in $recorded $listener; do
            printf 'pid=%s status=%q command=%q cwd=%q\n' "$pid" \
                "$(ps -p "$pid" -o stat= 2>/dev/null || true)" \
                "$(process_command "$pid")" "$(process_cwd "$pid")"
            if matches_service "$name" "$pid"; then say 'identity=matched'; else say 'identity=UNKNOWN'; fi
        done
    done
    printf 'execution_pids=%s\n' "$(execution_pids)"
}

case "${1:-start}" in
    start) start_all ;;
    stop) stop_all ;;
    restart) stop_all; start_all ;;
    reload-web)
        ensure_runtime
        # Reload only the dashboard code. Leave the signed execution worker,
        # its graceful-stop marker, and the read-only API untouched.
        stop_one "行情面板" "$WEB_PID_FILE" 5001
        start_web
        ;;
    status) status ;;
    logs) logs ;;
    diagnose-stop) diagnose_stop ;;
    *)
        cat <<'USAGE'
用法：./start.sh [start|stop|restart|reload-web|status|logs|diagnose-stop]

  start    启动行情面板和统一只读 API（默认）
  stop     全停：终止实盘执行进程，再停止行情面板和只读 API
  restart  重启本入口启动的服务
  reload-web  仅重载面板代码，保留实盘进程、原持仓监控及停止标记
  status   查看健康状态和进程
  logs     查看最近日志
  diagnose-stop  只读诊断停止时的进程身份识别

启动不会开启实盘，也不会提交订单。
USAGE
        exit 2
        ;;
esac
