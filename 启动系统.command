#!/bin/bash
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

if ! "$PYTHON" -c "import flask, pandas" >/dev/null 2>&1; then
    echo "当前 Python 环境缺少 Flask 或 Pandas。"
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
echo "浏览器地址：http://127.0.0.1:${PORT}/"
if [[ "$PORT" != "$START_PORT" ]]; then
    echo "默认端口 ${START_PORT} 已被占用，已自动改用 ${PORT}。"
fi
echo "保持此终端窗口打开；关闭窗口即可停止服务。"
echo

if [[ "${ZOUYE_NO_BROWSER:-0}" != "1" ]]; then
    (sleep 2; open "http://127.0.0.1:${PORT}/") >/dev/null 2>&1 &
fi

exec "$PYTHON" app.py
