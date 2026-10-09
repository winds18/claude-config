---
name: project-bootstrap
description: 为项目建立 Claude 协作入口：生成精简的项目 CLAUDE.md、.claude/settings.json（并行所需的 worktree 基线、权限、可选 hooks）、.worktreeinclude 与 gitignore 条目，并验证真实的安装/测试/启动命令。
when_to_use: 用户要求初始化项目的 Claude 配置、为并行开发准备仓库、或项目缺少可用的验证命令说明时。
argument-hint: "[项目目录，默认当前目录]"
disable-model-invocation: true
---

# 项目初始化

目标：让任何会话（含 worktree 子代理）都能找到真实的命令、边界与并行约定。只写不能从代码直接看出的事实。

## 步骤

1. **侦察**（只读）：读 README、包管理文件、CI 配置、Makefile/脚本、已有 CLAUDE.md / AGENTS.md / `.claude/`。已有内容优先保留与合并，不覆盖。
2. **验证命令**：实际运行安装、类型检查、单测、构建、启动（能跑的都跑一遍），只把成功验证过的命令写进 CLAUDE.md；失败的写明现状。
3. **项目 CLAUDE.md**：按 [templates/project-CLAUDE.md](templates/project-CLAUDE.md) 填写，删除不适用的段落，控制在 100 行内。仓库已用 AGENTS.md 时，CLAUDE.md 可用 `@AGENTS.md` 引入后只补 Claude 特有部分。
4. **`.claude/settings.json`**：参照 [templates/project-settings.json](templates/project-settings.json)：
   - 必须：`"worktree": {"baseRef": "head"}`，保证 worktree 子代理能看到本地检查点提交；
   - 权限：把已验证的测试/构建/lint 命令加入 `permissions.allow`，减少并行时的审批打断；
   - 可选：格式化/lint 的 PostToolUse hook，代理团队的 TaskCompleted 验收 hook（只在用户需要时启用）。
5. **`.worktreeinclude`**：列出 worktree 需要的 gitignored 文件（如 `.env.local`）。不要列入真实生产密钥。
6. **`.gitignore`**：加入 `.claude/worktrees/`、`.claude/settings.local.json`、`CLAUDE.local.md`、`.tmp/`（按需）。
7. **CI（项目没有时）**：参照 [templates/ci-github.yml](templates/ci-github.yml) 生成最小 CI，只放步骤 2 中验证通过的命令；会话内验证通过不代表合入时仍通过。已有 CI 时只核对其命令与 CLAUDE.md 一致。
8. **效率插件与过滤（可选）**：类型语言项目建议安装对应的代码智能插件（LSP），用"跳转定义"代替 grep + 多文件读取，编辑后自动报告类型错误；测试输出很长的项目可加一个 PreToolUse hook，用 `updatedInput` 把测试命令改写为只输出失败部分（示例见官方 costs 文档）。
9. **路径规则（可选）**：前后端或多语言仓库，把仅适用于某些路径的约定写进 `.claude/rules/<topic>.md` 并加 `paths:` frontmatter，而不是塞进根 CLAUDE.md。
10. **汇报**：列出新建/修改的文件、验证过的命令及结果、未能验证的部分。不自动提交。

## 不做

- 不生成 PRD/架构/任务列表等文档五件套；跨阶段项目确有需要时才用 [templates/plan.md](templates/plan.md)。
- 不复制全局规范到项目文件；项目文件只写项目事实和例外。
- 不重排成熟仓库的目录结构。
