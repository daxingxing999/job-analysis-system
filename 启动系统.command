#!/bin/bash
# ============================================================
#  招聘岗位需求分析与可视化系统 —— macOS / Linux 启动入口
#
#  与 Windows 的 启动系统.bat 保持一致的三件事：
#    1. 探测解释器（优先项目内 .venv）
#    2. 校验依赖，缺失时给出可直接复制的安装命令
#    3. **健康检查通过后再打开浏览器**，避免用户看到「无法访问」
#  端口可用 PORT 环境变量覆盖，默认 5000。
# ============================================================
set -euo pipefail

cd "$(dirname "$0")"
export PYTHONIOENCODING=utf-8
export NO_PROXY="${NO_PROXY:+${NO_PROXY},}127.0.0.1,localhost"
export no_proxy="${no_proxy:+${no_proxy},}127.0.0.1,localhost"

if [[ -x ".venv/bin/python" ]]; then
    PYTHON=".venv/bin/python"
elif command -v python3 >/dev/null 2>&1; then
    PYTHON="$(command -v python3)"
else
    echo "未找到 Python 3。请先从 https://www.python.org/downloads/macos/ 安装 Python 3。"
    read -r -p "按回车键关闭窗口..."
    exit 1
fi

# 抓取脚本与部分源码用到 3.10+ 的类型语法，版本太低要提前说清楚
if ! "$PYTHON" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)'; then
    echo "当前 Python 版本过低：$("$PYTHON" --version 2>&1)"
    echo "本项目需要 Python 3.10 及以上。"
    echo
    read -r -p "按回车键关闭窗口..."
    exit 1
fi

if ! "$PYTHON" -c "import flask, pandas" >/dev/null 2>&1; then
    echo "当前 Python 环境缺少 Flask 或 Pandas："
    echo "  $PYTHON"
    echo
    echo "请在项目目录打开终端，运行以下命令安装依赖："
    echo "  python3 -m venv .venv"
    echo "  .venv/bin/python -m pip install -r requirements.txt"
    echo
    read -r -p "按回车键关闭窗口..."
    exit 1
fi

START_PORT="${PORT:-5000}"
if ! PORT="$("$PYTHON" - "$START_PORT" <<'PY'
import socket
import sys

try:
    start = int(sys.argv[1])
except ValueError:
    raise SystemExit("PORT 必须是有效的端口号")

for port in range(start, 65536):
    try:
        with socket.socket() as server:
            server.bind(("0.0.0.0", port))
    except OSError:
        continue
    print(port)
    break
else:
    raise SystemExit("没有可用的 TCP 端口")
PY
)"; then
    echo "无法找到可用端口，请检查 PORT 设置。"
    read -r -p "按回车键关闭窗口..."
    exit 1
fi
export PORT

echo
echo "招聘岗位分析系统正在启动..."
echo "解释器：$PYTHON"
echo "浏览器地址：http://127.0.0.1:${PORT}/"
if [[ "$PORT" != "$START_PORT" ]]; then
    echo "默认端口 ${START_PORT} 已被占用，已自动改用 ${PORT}。"
fi
echo "保持此终端窗口打开；关闭窗口即可停止服务。"
echo

"$PYTHON" app.py &
SERVER_PID=$!

# 关窗即停：把子进程一起带走
cleanup() {
    kill "$SERVER_PID" >/dev/null 2>&1 || true
}
trap cleanup EXIT INT TERM

# 健康检查：等端口真正可连接再开浏览器
READY=0
for _ in $(seq 1 30); do
    if ! kill -0 "$SERVER_PID" >/dev/null 2>&1; then
        echo
        echo "服务进程已退出。若提示端口被占用，可换一个端口重试："
        echo "  PORT=5001 ./启动系统.command"
        echo
        wait "$SERVER_PID" || true
        exit 1
    fi
    if "$PYTHON" - "$PORT" <<'PY' >/dev/null 2>&1
import socket
import sys

with socket.socket() as client:
    client.settimeout(0.6)
    raise SystemExit(0 if client.connect_ex(("127.0.0.1", int(sys.argv[1]))) == 0 else 1)
PY
    then
        READY=1
        break
    fi
    sleep 1
done

if [[ "$READY" != "1" ]]; then
    echo "等待 30 秒仍未监听 ${PORT} 端口，服务可能启动失败。请查看上面的输出。"
    exit 1
fi

if [[ "${ZOUYE_NO_BROWSER:-0}" != "1" ]]; then
    open "http://127.0.0.1:${PORT}/" >/dev/null 2>&1 || true
fi

# 把服务进程留在前台，Ctrl-C 或关窗时由 trap 收尾
wait "$SERVER_PID"
