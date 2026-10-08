#!/usr/bin/env bash
# 完整校验：静态检查 + 守卫行为测试 + 临时目录安装/卸载往返（复制与软链接两种模式）。
# 不触碰真实 ~/.claude。
#   bash scripts/validate.sh                    # 全量
#   bash scripts/validate.sh --changed [<base>] # 静态检查 + 相对 base（默认 HEAD，含未提交/未跟踪）受影响的测试组；
#                                               # 无法归类的改动或无法取得改动时跑全量（映射见 validate.py select_groups）
set -euo pipefail
root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
all_groups="guard parallel integrate workflow self_update dispatch validate install"
mode=all base=HEAD
case "${1:-}" in
  "") ;;
  --changed) mode=changed; base="${2:-HEAD}"; [[ $# -le 2 ]] || { echo "用法: validate.sh [--changed [<base>]]" >&2; exit 2; } ;;
  *) echo "用法: validate.sh [--changed [<base>]]" >&2; exit 2 ;;
esac
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
fail() { echo "  FAIL $*" >&2; exit 1; }

echo "[static] 静态检查（含交叉引用）"
python3 "$root/scripts/validate.py"

if [[ $mode == changed ]]; then
  sel="$(python3 "$root/scripts/validate.py" --changed-groups "$base")"
  [[ "$sel" == all ]] && sel="$all_groups"
else
  sel="$all_groups"
fi
ran="" skipped=""
want() { [[ " $sel " == *" $1 "* ]]; }
# group <名称> <说明> <命令…>：被选中则运行，否则记为跳过
group() {
  local name="$1" label="$2"; shift 2
  if want "$name"; then echo "[$name] $label"; "$@"; ran="$ran $name"; else skipped="$skipped $name"; fi
}

run_workflow_tests() {
  if command -v node >/dev/null; then node "$root/scripts/test_workflows.mjs"; else echo "  跳过：未安装 node（workflow 未验证）"; fi
}
run_dispatch_tests() {
  python3 "$root/scripts/test_dispatch.py"   # 缺失即失败：文件已入库，兜底只会掩盖测试被删
}

# 构造一个"已有用户配置"的 home
seed() {
  local h="$1"
  mkdir -p "$h/agents/zcf" "$h/rules" "$h/skills/mine"
  printf '{\n  "env": {"KEEP": "1"},\n  "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "true"}]}]}\n}\n' > "$h/settings.json"
  printf -- '---\nname: planner\ndescription: user own\n---\nmine\n' > "$h/agents/zcf/planner.md"
  echo "legacy" > "$h/rules/agents.md"
  echo "# mine" > "$h/CLAUDE.md"
  echo "skill" > "$h/skills/mine/SKILL.md"
  mkdir -p "$h/skills/dev-workflow" && echo "user skill" > "$h/skills/dev-workflow/SKILL.md"
  cp "$h/settings.json" "$h/.settings.orig"
}
inst() { python3 "$root/scripts/dev_spec_install.py" "$@" --skip-version-check >/dev/null; }
snapshot() { (cd "$1" && find . -not -path './dev-spec-backups*' -not -name .settings.orig | sort | while read -r f; do
  if [[ -L "$f" ]]; then echo "L $f"; elif [[ "$f" == ./settings.json ]]; then echo "F $f (语义比较)"; elif [[ -f "$f" ]]; then echo "F $f $(shasum < "$f" | cut -c1-12)"; else echo "D $f"; fi; done); }

roundtrip() {
  local mode="$1" h="$tmp/home-$1"
  seed "$h"
  local before; before="$(snapshot "$h")"
  inst install "--$mode" --claude-home "$h"
  [[ ! -e "$h/.dev-spec-manifest.json" ]] || fail "$mode: 预览写入了文件"

  inst install "--$mode" --apply --claude-home "$h"
  grep -q "user skill" "$h/skills/dev-workflow/SKILL.md" || fail "$mode: 未经 --force 替换了用户技能"
  grep -q "# mine" "$h/CLAUDE.md" || fail "$mode: 未经 --manage-claude-md 替换了 CLAUDE.md"
  [[ -f "$h/rules/agents.md" ]] || fail "$mode: 未经 --retire 移走了旧规则"

  inst install "--$mode" --apply --force --manage-claude-md --retire-legacy-rules --claude-home "$h"
  inst install "--$mode" --apply --force --manage-claude-md --retire-legacy-rules --claude-home "$h"   # 幂等
  [[ -f "$h/rules/dev-spec/01-core.md" && -f "$h/agents/dev-spec/implementer.md" ]] || fail "$mode: 组件缺失"
  [[ -f "$h/skills/parallel-dev/SKILL.md" && -f "$h/hooks/dev-spec/policy-guard.py" ]] || fail "$mode: 组件缺失"
  [[ -f "$h/skills/parallel-dev/scripts/integrate.py" && -f "$h/workflows/dev-spec-implement.js" && -f "$h/workflows/dev-spec-review.js" ]] || fail "$mode: 集成脚本或 workflow 缺失"
  [[ ! -e "$h/rules/agents.md" ]] || fail "$mode: 旧规则未退役"
  grep -q "简体中文" "$h/CLAUDE.md" || fail "$mode: CLAUDE.md 未接管"
  [[ -f "$h/agents/zcf/planner.md" && -f "$h/skills/mine/SKILL.md" ]] || fail "$mode: 动了无关文件"
  if [[ "$mode" == link ]]; then
    [[ -L "$h/rules/dev-spec" && -L "$h/CLAUDE.md" ]] || fail "link: 不是软链接"
  else
    [[ ! -L "$h/rules/dev-spec" ]] || fail "copy: 不应是软链接"
  fi
  python3 - "$h" <<'PY' || fail "$mode: settings 合并错误"
import json, sys
s = json.load(open(sys.argv[1] + "/settings.json"))
assert s["env"]["KEEP"] == "1" and "Stop" in s["hooks"]
assert sum("dev-spec/policy-guard.py" in h["command"] for g in s["hooks"]["PreToolUse"] for h in g["hooks"]) == 1
assert s["worktree"]["baseRef"] == "head"
PY
  python3 "$root/scripts/dev_spec_install.py" doctor --skip-version-check --claude-home "$h" >/dev/null || fail "$mode: doctor 报错"

  # 守卫脚本缺失（如外接盘未挂载）时 hook 命令必须放行（exit 0），不能 exit 2 阻断所有工具
  local cmd; cmd="$(python3 -c "import json,sys; s=json.load(open(sys.argv[1])); print(s['hooks']['PreToolUse'][0]['hooks'][0]['command'])" "$h/settings.json")"
  echo '{"tool_name":"Bash","tool_input":{"command":"rm -rf /"}}' | bash -c "$cmd" | grep -q deny || fail "$mode: hook 命令未生效"
  mv "$h/hooks/dev-spec" "$h/hooks/dev-spec.off"
  echo '{"tool_name":"Bash","tool_input":{"command":"ls"}}' | bash -c "$cmd" || fail "$mode: 脚本缺失时 hook 未放行"
  mv "$h/hooks/dev-spec.off" "$h/hooks/dev-spec"

  inst uninstall --apply --claude-home "$h"
  [[ "$(snapshot "$h")" == "$before" ]] || { diff <(echo "$before") <(snapshot "$h") >&2; fail "$mode: 卸载后未还原"; }
  python3 -c "import json,sys; assert json.load(open(sys.argv[1]))==json.load(open(sys.argv[2]))" "$h/settings.json" "$h/.settings.orig" \
    || fail "$mode: settings 未还原"
  echo "  $mode 模式: 通过"
}

install_roundtrips() {
  echo "  复制模式"; roundtrip copy
  echo "  软链接模式"; roundtrip link
}

group guard "守卫行为测试" python3 "$root/scripts/test_policy_guard.py"
group parallel "并行守卫测试（真实 git worktree）" python3 "$root/scripts/test_parallel_guards.py"
group integrate "集成脚本测试（真实 git worktree）" python3 "$root/scripts/test_integrate.py"
group workflow "workflow 脚本测试（模拟运行时）" run_workflow_tests
group self_update "自我更新端到端测试（bare origin + copy/link 安装）" python3 "$root/scripts/test_self_update.py"
group dispatch "dispatch 测试" run_dispatch_tests
group validate "静态检查自测（注入坏引用、增量映射）" python3 "$root/scripts/test_validate.py"
group install "安装往返（复制 / 软链接）" install_roundtrips

echo "已运行: static${ran}"
echo "已跳过:${skipped:- 无}"
if [[ $mode == all || "$sel" == "$all_groups" ]]; then echo "全部校验通过"
elif [[ -z "$sel" ]]; then echo "相对 ${base} 无受影响的测试组：只运行了静态检查（不等于已验证功能；提交前跑全量）"
else echo "增量校验通过（相对 ${base}）；提交前与发布前仍需全量"; fi
