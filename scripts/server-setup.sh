#!/usr/bin/env bash
# 把 dev-spec 装到一台只有 git + python3（≥3.9）的服务器上，并交给自动更新维护。可重复执行。
#   bash scripts/server-setup.sh                  # 在服务器上直接运行
#   bash install.sh remote user@host [--port N]   # 从开发机经 ssh 运行（脚本走 stdin：bash -s）
# 环境变量：
#   DEV_SPEC_REPO_URL   规范仓库（默认 https://github.com/winds18/claude-config.git）
#   DEV_SPEC_DIR        克隆位置（默认 ~/.local/share/claude-config）
#   CLAUDE_CONFIG_DIR   Claude 配置目录（默认 ~/.claude）
#   DEV_SPEC_KEEP_EXISTING=1  首次安装时不接管已有的 CLAUDE.md、不退役 rules/ 下已有规则
# 行为：
#   首次  克隆 → 把默认分支放到语义版本最高的发布 tag（vX.Y.Z；还没有 tag 就留在分支最新提交）
#         → 直接用克隆里的安装器以 copy 模式安装（不经过该版本的 install.sh / validate.sh：发布 tag 已由 CI 校验）→ doctor
#   之后  已安装：只运行更新器（stable 通道跟随发布 tag）；有克隆但没装上：继续安装
#   首次安装默认接管 CLAUDE.md 并把 rules/ 下原有规则移入 dev-spec-backups/（uninstall 可还原）。
#   退出码：安装失败、doctor 有问题、或更新被拒绝/失败时非 0。
#   已存在的克隆不 reset、不重新克隆；已有文件一律不删除；缺依赖时在创建任何目录之前退出，本脚本不调用 sudo。
# 全部逻辑在 main 里、最后一行才调用：经 stdin 读入时 bash 先读完整个脚本再执行，且 main 的 stdin 接 /dev/null，
# 子进程读不到（也就吞不掉）脚本正文。不依赖 $0 / BASH_SOURCE。
set -euo pipefail

STAGING="" DONE=0
# 删除未完成的临时克隆并保住退出码。macOS 自带的 bash 3.2 在"未定义变量"中止时会带着 0 进入 EXIT trap，
# 所以没有走到结尾（DONE=1）的 0 一律改成 1：脚本不能半途而废却报告成功。
cleanup() {
  local rc=$?
  [[ -z "$STAGING" ]] || rm -rf "$STAGING"
  if [[ $rc == 0 && $DONE != 1 ]]; then rc=1; fi
  exit "$rc"
}
trap cleanup EXIT

say() { printf '%s\n' "$*"; }
die() { printf '错误：%s\n' "$*" >&2; exit 1; }

check_deps() {
  local missing="" ver=""
  command -v git >/dev/null 2>&1 || missing="git"
  if command -v python3 >/dev/null 2>&1; then
    ver="$(python3 --version 2>&1)" || ver=""
    # 主、次版本按整数比较：3.10 / 3.11 高于 3.9
    if ! [[ "$ver" =~ ([0-9]+)\.([0-9]+) ]]; then
      missing="${missing:+${missing}、}python3 ≥ 3.9（无法识别版本：${ver:-无输出}）"
    elif (( 10#${BASH_REMATCH[1]} < 3 || (10#${BASH_REMATCH[1]} == 3 && 10#${BASH_REMATCH[2]} < 9) )); then
      missing="${missing:+${missing}、}python3 ≥ 3.9（当前 ${ver#Python }）"
    fi
  else
    missing="${missing:+${missing}、}python3 ≥ 3.9"
  fi
  [[ -n "$missing" ]] || return 0
  printf '缺少依赖：%s\n请先安装再重跑（本脚本不会调用 sudo）：\n  sudo apt-get install -y git python3\n' "$missing" >&2
  exit 1
}

# 语义版本最高的 vX.Y.Z tag；rc 等其他形式忽略。与 hooks/dev_spec_update.py 的 latest_release_tag 同义。
latest_release_tag() {
  local t best="" a=-1 b=-1 c=-1 x y z
  while IFS= read -r t; do
    [[ "$t" =~ ^v([0-9]+)\.([0-9]+)\.([0-9]+)$ ]] || continue
    x=$((10#${BASH_REMATCH[1]})) y=$((10#${BASH_REMATCH[2]})) z=$((10#${BASH_REMATCH[3]}))
    if (( x > a || (x == a && (y > b || (y == b && z > c))) )); then best="$t" a=$x b=$y c=$z; fi
  done < <(git -C "$1" tag --list 'v*')
  printf '%s' "$best"
}

# 全新克隆：先在旁边的临时目录里克隆并对齐到发布 tag，全部就绪后才改名为目标目录，
# 所以目标目录要么不存在，要么是一份完整且已对齐的克隆（中途失败不会留下半成品被下次当成"已有克隆"）。
clone_fresh() {
  local url="$1" dir="$2" branch tag
  mkdir -p "$(dirname "$dir")"
  [[ ! -e "$dir.partial.$$" ]] || die "临时目录已存在：${dir}.partial.$$"
  STAGING="$dir.partial.$$"
  say "==> 克隆 ${url}"
  git clone --quiet "$url" "$STAGING" || die "克隆失败：${url}"
  branch="$(git -C "$STAGING" symbolic-ref -q --short HEAD)" || die "远端仓库没有可用的默认分支：${url}"
  tag="$(latest_release_tag "$STAGING")"
  if [[ -n "$tag" ]]; then
    # 留在分支上并保留上游，只把分支放到 tag：stable 通道的更新器之后才能 fast-forward 到新 tag
    git -C "$STAGING" reset --quiet --hard "refs/tags/$tag^{commit}"
    say "    使用发布版本 ${tag}（分支 ${branch}）"
  else
    say "    还没有发布 tag，使用 ${branch} 分支的最新提交"
  fi
  [[ ! -d "$dir" ]] || rmdir "$dir"          # 只可能是空目录（调用前已确认）
  mv "$STAGING" "$dir"
  STAGING=""
}

main() {
  check_deps
  local url="${DEV_SPEC_REPO_URL:-https://github.com/winds18/claude-config.git}"
  local dir="${DEV_SPEC_DIR:-$HOME/.local/share/claude-config}"
  local conf="${CLAUDE_CONFIG_DIR:-$HOME/.claude}"
  [[ "$dir" == /* ]] || dir="$PWD/$dir"
  [[ "$conf" == /* ]] || conf="$PWD/$conf"
  # 去掉尾部斜杠：否则临时克隆会落在目标目录内部，首次安装必然失败
  while [[ "$dir" == */ && "$dir" != / ]]; do dir="${dir%/}"; done
  while [[ "$conf" == */ && "$conf" != / ]]; do conf="${conf%/}"; done
  export CLAUDE_CONFIG_DIR="$conf" GIT_TERMINAL_PROMPT=0

  local manifest="$conf/.dev-spec-manifest.json" installed=0 fresh=0 origin_dir="" rc=0
  if [[ -f "$manifest" ]]; then
    # 已有安装必须来自同一个克隆目录，否则更新器维护的是另一份源，这里不接管
    origin_dir="$(python3 -c 'import json, os, sys
src = json.load(open(sys.argv[1])).get("source", "")
print(src)
sys.exit(0 if src and os.path.realpath(src) == os.path.realpath(sys.argv[2]) else 3)' "$manifest" "$dir")" || rc=$?
    case $rc in
      0) installed=1 ;;
      3) die "${conf} 已有来自 ${origin_dir:-未知位置} 的安装，不是 ${dir}。把 DEV_SPEC_DIR 指向原位置，或先卸载再运行；未做任何修改" ;;
      *) die "无法读取安装清单 ${manifest}；未做任何修改" ;;
    esac
  fi

  if [[ -e "$dir/.git" ]] && git -C "$dir" rev-parse --verify -q HEAD >/dev/null 2>&1; then
    say "==> 使用已有克隆 ${dir}（不 reset、不重新克隆）"
  elif [[ ! -e "$dir" && ! -L "$dir" ]] || { [[ -d "$dir" ]] && [[ -z "$(ls -A "$dir")" ]]; }; then
    clone_fresh "$url" "$dir"
    fresh=1
  else
    die "${dir} 已存在但不是可用的 git 克隆；未做任何修改。请移走它，或把 DEV_SPEC_DIR 指向别处"
  fi

  if [[ $installed == 1 && $fresh == 0 ]]; then
    say "==> 已安装，检查更新"
    local updater="$conf/hooks/dev-spec/dev_spec_update.py"
    [[ -f "$updater" ]] || updater="$dir/hooks/dev_spec_update.py"
    python3 "$updater" --now --verbose
    # 更新器自身恒返回 0（不能打断会话）；这里读它记录的结果，被拒绝或失败时让本脚本非 0，批量部署才不会假绿
    local result
    result="$(python3 -c 'import json, sys
try:
    print(json.load(open(sys.argv[1])).get("last_result", ""))
except Exception:
    print("")' "$conf/dev-spec-update.json")"
    case "$result" in 拒绝*|失败*) rc=1 ;; esac
  elif [[ $installed == 1 ]]; then
    # 清单还在但克隆是新建的（原克隆被移走）：按记住的选项重装，不重复一次性的迁移
    say "==> 克隆已重建，按原选项重新安装"
    python3 "$dir/scripts/dev_spec_install.py" install --copy --apply || die "安装失败；修复后可重跑本脚本"
  else
    say "==> 安装（copy 模式，自动更新跟随发布 tag）"
    # 默认接管 CLAUDE.md 并把 rules/ 下原有规则移入备份（可用 uninstall 还原）；DEV_SPEC_KEEP_EXISTING=1 时两者都不动
    local takeover=(--manage-claude-md --retire-legacy-rules)
    [[ "${DEV_SPEC_KEEP_EXISTING:-}" == 1 ]] && takeover=()
    python3 "$dir/scripts/dev_spec_install.py" install --copy ${takeover[@]+"${takeover[@]}"} --apply \
      || die "安装失败；克隆保留在 ${dir}，修复后可重跑本脚本"
  fi

  say "==> 状态"
  python3 "$dir/scripts/dev_spec_install.py" doctor || rc=1
  DONE=1
  return "$rc"
}

main "$@" </dev/null
