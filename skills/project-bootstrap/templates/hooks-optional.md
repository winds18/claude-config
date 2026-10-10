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

## 原则

- 不用关键词、回复长度或格式判断"是否完成"（Stop hook 不做这种门禁）。
- 新增阻断须对应具体故障、低误报并能恢复；hook 是补充检查，不是权限边界。
