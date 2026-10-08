#!/usr/bin/env bash
# dev-spec 安装入口。默认只预览，加 --apply 才写入。
#   bash install.sh                      # 预览安装到 ~/.claude
#   bash install.sh --apply              # 安装
#   bash install.sh doctor               # 检查已安装状态
#   bash install.sh uninstall --apply    # 卸载并恢复备份
#   bash install.sh --claude-home DIR    # 安装到其他配置目录（测试/多账号）
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
command -v python3 >/dev/null || { echo "需要 python3 (3.9+)" >&2; exit 1; }
if [[ "${1:-}" != "uninstall" && "${1:-}" != "doctor" ]]; then
  bash "$here/scripts/validate.sh" >/dev/null || { echo "源码校验失败，先运行 bash scripts/validate.sh 查看详情" >&2; exit 1; }
fi
exec python3 "$here/scripts/dev_spec_install.py" "$@"
