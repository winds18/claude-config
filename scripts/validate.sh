#!/usr/bin/env bash
# 完整校验：静态检查 + 守卫行为测试 + 临时目录安装/卸载往返（复制与软链接两种模式）。
# 不触碰真实 ~/.claude。
set -euo pipefail
root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
fail() { echo "  FAIL $*" >&2; exit 1; }

echo "[1/5] 静态检查"
python3 "$root/scripts/validate.py"

echo "[2/5] 守卫行为测试"
python3 "$root/scripts/test_policy_guard.py"

echo "[3/5] 并行守卫测试（真实 git worktree）"
python3 "$root/scripts/test_parallel_guards.py"

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

echo "[4/5] 安装往返：复制模式"
roundtrip copy
echo "[5/5] 安装往返：软链接模式"
roundtrip link
echo "全部校验通过"
