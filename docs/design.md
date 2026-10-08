# 设计说明：从 codex-config 到 dev-spec

参考来源：[winds18/codex-config](https://github.com/winds18/codex-config)。本规范保留其工程原则，把执行机制换成 Claude Code 的原生能力，去掉 Codex 专属或对 Claude 无收益的部分。

## 分层与加载成本

| 层 | 位置（安装后） | 何时进入上下文 | 放什么 |
| --- | --- | --- | --- |
| 常驻规则 | `~/.claude/rules/dev-spec/*.md` | 每个会话 | 跨任务原则、硬规则（≤200 行，validate 强制） |
| 技能 | `~/.claude/skills/<name>/` | 仅描述常驻；正文按需加载 | 多步流程、模板、表格 |
| 子代理 | `~/.claude/agents/*.md` | 委派时 | 角色指令、工具限制、模型与 effort |
| Hook | `~/.claude/hooks/dev-spec/` + `settings.json` | 每次匹配的工具调用 | 低误报的确定性检查 |
| 项目层 | 项目 `CLAUDE.md`、`.claude/settings.json`、`.claude/rules/` | 项目会话 | 真实命令、边界、例外 |

规则放 `rules/dev-spec/` 子目录而不是写进 `~/.claude/CLAUDE.md`：不覆盖个人文件，可整体安装/卸载，且与个人偏好互不干扰。

## 保留并改写的部分

| codex-config 原则 | dev-spec 中的落点 | Claude 机制 |
| --- | --- | --- |
| 授权内自主推进，只问无法判断的歧义 | `01-core` 自主推进 | AskUserQuestion、Plan 模式 |
| 需求 → 架构 → 契约 → 并行实现 → 集成验证 | `02-engineering` + `dev-workflow` 技能 | 技能按需加载 |
| 算法/性能/结构质量要求与"有证据的优化" | `dev-workflow` 质量表 | — |
| 测试矩阵、不弱化验收、证据绑定当前状态 | `02-engineering`、`dev-workflow` | `/code-review`、`/simplify`、`/security-review` |
| 主任务走关键路径、有收益才委派 | `03-parallel` | Agent 工具、后台运行、完成通知 |
| 单一写入负责人、改派先确认停止 | `03-parallel` 硬规则、`parallel-dev` §6 | `isolation: worktree`、TaskStop |
| 工作包六要素 | `parallel-dev/references/work-package.md` | 子代理不继承历史，模板强制自包含 |
| 多端按职责组织、联合验收 | `parallel-dev` §7–8 | 代理团队、共享任务列表、SendMessage |
| 跨仓逐仓核对规则与版本 | `parallel-dev` §8 | `--add-dir`、多会话 |
| 临时文件隔离与清理 | `02-engineering` | 会话 scratchpad |
| 提交/推送/发布分别授权 | `04-git-delivery` | 权限模式 |
| Hook 只做低误报确定性阻断，不用关键词判完成 | `hooks/policy-guard.py`、`hooks-optional.md` | PreToolUse `deny`/`ask` |
| 项目模板短而具体，不要文档五件套 | `project-bootstrap` 技能 | 项目 CLAUDE.md、`.claude/rules/` paths |
| 安装默认预览、事务清单、可卸载 | `install.sh` | — |

## Claude 特有的新增

- **worktree 基线陷阱**：子代理 worktree 默认从远端默认分支创建，看不到本地检查点。规范要求项目设置 `worktree.baseRef: "head"` 并在派发前提交契约检查点；`.worktreeinclude` 带入 gitignored 环境文件。
- **集成协议**：worktree 子代理在自己分支提交并回报 SHA，主会话检查越界 diff 后按依赖顺序合入，再在集成状态上验证。
- **只读角色的硬约束**：复核类子代理用 `tools` + `disallowedTools` 保证不能写文件，而不是只靠指令。
- **代理团队质量门**：`TaskCompleted` hook 以 exit 2 拒绝未通过验收的任务完成。
- **`ask` 决策**：Claude 的 PreToolUse 支持 `ask`，危险但可能合理的 git 操作交由用户确认，而不是一律拒绝。
- **上下文经济**：大范围检索交给 Explore；长任务用后台运行与完成通知，禁止轮询。

## 刻意舍弃

- Codex 专属：TOML 代理、Luna/Terra 模型配置、原生 Goal、`AGENTS.override.md`、`$skill` 调用语法、`apply_patch` 守卫、客户端兼容文档。
- 与内置能力重复的角色：`explorer-lite`、`planner`（内置 Explore/Plan）、`summarizer-lite`、`refiner`、`pr-preparer`、`security-lite`、`orchestrator`（主会话即编排者）。
- 审计目录、设计提示词技能（与开发规范无关）。
- 每次提交的完整 pre-push 历史扫描：改为 PreToolUse 在 `git commit` 时扫描暂存新增行；需要服务端级别保护时用仓库自己的 CI/secret scanning。
- 固定覆盖率门槛（如 80%）、强制 TDD、"写代码前必须 GitHub 搜索"：改为按行为与风险选择验证，复用优先但不设仪式。

## 维护约定

- 新增常驻规则必须对应具体故障，有适用范围和可观察的遵守方式；细节放技能，只维护一处。
- 改动后运行 `bash scripts/validate.sh`；它不会触碰真实的 `~/.claude`。
- 新增 hook 阻断须附带正例、反例（合法参数、带空格路径、引号中的文本）测试。
