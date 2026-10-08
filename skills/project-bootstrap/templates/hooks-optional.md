# 可选的项目级 hooks

按需合并到项目 `.claude/settings.json` 的 `hooks` 字段。只在用户确认需要时启用。

## 编辑后格式化（PostToolUse，不阻断）

```json
{
  "PostToolUse": [
    {
      "matcher": "Edit|Write",
      "hooks": [
        { "type": "command", "command": "<格式化命令，例如 npx prettier --write \"$(jq -r .tool_input.file_path)\">", "timeout": 30 }
      ]
    }
  ]
}
```

## 代理团队任务验收门（TaskCompleted，exit 2 拒绝完成）

脚本运行该仓库的快速验收命令；失败时把输出写到 stderr 并 `exit 2`，队友会收到反馈并继续修复。

```json
{
  "TaskCompleted": [
    {
      "hooks": [
        { "type": "command", "command": "\"$CLAUDE_PROJECT_DIR\"/.claude/hooks/task-gate.sh", "timeout": 600 }
      ]
    }
  ]
}
```

`task-gate.sh` 示例：

```bash
#!/usr/bin/env bash
set -uo pipefail
cd "$(jq -r '.cwd // empty')" 2>/dev/null || true
if ! out=$(<快速验收命令> 2>&1); then
  printf '验收未通过，先修复再标记完成：\n%s\n' "$(printf '%s' "$out" | tail -40)" >&2
  exit 2
fi
```

## 原则

- 不用关键词、回复长度或格式判断"是否完成"（Stop hook 不做这种门禁）。
- 新增阻断须对应具体故障、低误报并能恢复；hook 是补充检查，不是权限边界。
