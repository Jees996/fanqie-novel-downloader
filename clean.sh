#!/usr/bin/env bash
# 番茄小说一键下载器 - 环境清理脚本
# 清理运行时生成的内容，把项目恢复到初始状态；源码文件不受影响。
set -u
cd "$(dirname "$0")"

echo "============================================"
echo "  番茄小说一键下载器 - 环境清理"
echo "============================================"
echo
echo "将删除以下「运行时生成」的内容（源码文件不受影响）："
echo "   [1] .venv / venv          虚拟环境"
echo "   [2] __pycache__ / *.pyc   Python 字节码缓存"
echo "   [3] downloads/*.txt       已导出的 TXT 文件"
echo "   [4] .DS_Store / Thumbs.db 系统临时文件"
echo "   [5] .fanqie_device.json   匿名设备 / 正文密钥缓存"
echo

printf "确认清理请输入 Y 后回车（直接回车取消）: "
CONFIRM=""
read -r CONFIRM || true
if [ "$CONFIRM" != "Y" ] && [ "$CONFIRM" != "y" ]; then
    echo
    echo "已取消，未做任何修改。"
    exit 0
fi

echo
echo "正在清理..."

if [ ! -e ".venv" ] && [ ! -e "venv" ]; then
    echo "[1/5] 未发现虚拟环境，无需清理"
fi

for dir in .venv venv; do
    if [ -e "$dir" ]; then
        if rm -rf "$dir" 2>/dev/null; then
            echo "[1/5] 已删除 $dir"
        else
            echo "[警告] $dir 删除失败，可能有程序正在使用它。"
            echo "       请先关闭正在运行的服务窗口后重试。"
        fi
    fi
done

find . -type d -name '__pycache__' -prune -exec rm -rf {} + 2>/dev/null
echo "[2/5] 已清理 __pycache__"

find . -type f -name '*.pyc' -delete 2>/dev/null
echo "[2/5] 已清理 *.pyc"

if [ -d "downloads" ]; then
    find downloads -type f -name '*.txt' -delete 2>/dev/null
fi
echo "[3/5] 已清空 downloads 目录中的 TXT"

find . -type f \( -name '.DS_Store' -o -name 'Thumbs.db' \) -delete 2>/dev/null
echo "[4/5] 已清理系统临时文件"

device_cache_removed=0
for f in .fanqie_device.json .fanqie_device.tmp; do
    if [ -e "$f" ]; then
        rm -f "$f" 2>/dev/null && device_cache_removed=1
    fi
done
if [ "$device_cache_removed" -eq 1 ]; then
    echo "[5/5] 已删除设备密钥缓存"
else
    echo "[5/5] 未发现设备密钥缓存，无需清理"
fi

echo
echo "============================================"
echo "  [完成] 运行时环境与导出文件已清理，"
echo "         项目已恢复到初始状态。"
echo
echo "  如需完全清理：关闭本终端，直接删除"
echo "  整个文件夹即可。"
echo
echo "  本工具不写注册表、不安装系统服务、"
echo "  不修改全局 Python，删除文件夹后"
echo "  系统不会留下任何残留。"
echo "============================================"
