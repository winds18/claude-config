#!/usr/bin/env bash
# dev-spec 安装入口。默认只预览，加 --apply 才写入。
#   bash install.sh                      # 预览安装到 ~/.claude
#   bash install.sh --apply              # 安装（模式与选项会被记住，自动更新默认开启）
#   bash install.sh doctor               # 检查安装状态与最近一次自动更新
#   bash install.sh update               # 立即检查更新：fetch → 校验候选版本 → fast-forward → 重装
#   bash install.sh rollback vX.Y.Z      # 设备回滚到某个发布版本并关闭自动更新（copy 模式）
#   bash install.sh resume               # 结束回滚：切回原分支、重新开启自动更新
#   bash install.sh uninstall --apply    # 卸载并恢复备份
#   bash install.sh --claude-home DIR    # 安装到其他配置目录（测试/多账号）
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
command -v python3 >/dev/null || { echo "需要 python3 (3.9+)" >&2; exit 1; }
case "${1:-}" in
  update)
    shift
    home="${CLAUDE_CONFIG_DIR:-$HOME/.claude}"
    while [[ $# -gt 0 ]]; do
      case "$1" in --claude-home) home="$2"; shift 2 ;; *) echo "未知参数：$1" >&2; exit 2 ;; esac
    done
    CLAUDE_CONFIG_DIR="$home" exec python3 "$here/hooks/dev_spec_update.py" --now --verbose ;;
  uninstall|doctor|rollback|resume) ;;
  *) bash "$here/scripts/validate.sh" >/dev/null || { echo "源码校验失败，先运行 bash scripts/validate.sh 查看详情" >&2; exit 1; } ;;
esac
exec python3 "$here/scripts/dev_spec_install.py" "$@"
