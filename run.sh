#!/usr/bin/env bash
# 番茄小说一键下载器 - Linux / macOS 启动脚本
set -e
cd "$(dirname "$0")"

echo "============================================"
echo "  番茄小说一键下载器  v1.1.0"
echo "============================================"
echo

# ---------- 1. 查找 Python 3.9+ ----------
PYTHON_BIN=""
for candidate in python3 python; do
    if command -v "$candidate" >/dev/null 2>&1; then
        if "$candidate" -c "import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)" >/dev/null 2>&1; then
            PYTHON_BIN="$candidate"
            break
        fi
    fi
done

if [ -z "$PYTHON_BIN" ]; then
    echo "[错误] 未检测到 Python 3.9 及以上版本，请先安装。"
    echo "       下载地址: https://www.python.org/downloads/"
    exit 1
fi

# ---------- 2. 创建虚拟环境（失败时回退系统 Python） ----------
VENV_DIR=".venv"
VENV_PY="$VENV_DIR/bin/python"

if [ ! -x "$VENV_PY" ]; then
    echo "[1/3] 首次运行，正在创建虚拟环境 .venv ..."
    if ! "$PYTHON_BIN" -m venv "$VENV_DIR" >/dev/null 2>&1; then
        echo "[警告] 虚拟环境创建失败，将直接使用系统 Python。"
        echo "       Debian/Ubuntu 用户可执行: sudo apt install python3-venv"
    fi
fi

PY_RUN="$VENV_PY"
if [ ! -x "$PY_RUN" ]; then
    PY_RUN="$PYTHON_BIN"
fi

if ! "$PY_RUN" -m pip --version >/dev/null 2>&1; then
    echo "[提示] 当前 Python 缺少 pip，正在尝试修复..."
    "$PY_RUN" -m ensurepip --upgrade >/dev/null 2>&1 || true
    if ! "$PY_RUN" -m pip --version >/dev/null 2>&1; then
        echo "[警告] 无法修复 pip，将直接使用系统 Python。"
        PY_RUN="$PYTHON_BIN"
    fi
fi

# ---------- 3. 安装依赖（清华镜像） ----------
MIRROR="https://pypi.tuna.tsinghua.edu.cn/simple"
echo "[2/3] 正在检查依赖（首次安装可能需要 1-2 分钟）..."
if ! "$PY_RUN" -m pip install --disable-pip-version-check -r requirements.txt -i "$MIRROR" -q; then
    echo "[警告] 清华镜像安装失败，正在尝试官方源..."
    if ! "$PY_RUN" -m pip install --disable-pip-version-check -r requirements.txt -q; then
        echo "[错误] 依赖安装失败，请检查网络后重试。"
        exit 1
    fi
fi

# ---------- 4. 启动服务（浏览器由 app.py 自动打开） ----------
echo "[3/3] 正在启动服务..."
echo "浏览器将自动打开，如未打开请手动访问: http://127.0.0.1:8000"
echo "按 Ctrl+C 可停止服务。"
echo

exec "$PY_RUN" app.py
