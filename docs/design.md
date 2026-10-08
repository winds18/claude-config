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
| 单一写入负责人、改派先确认停止 | `03-parallel` 硬规则、`parallel-dev` §5 | `isolation: worktree`、TaskStop |
| 工作包六要素 | `parallel-dev/references/work-package.md` | 子代理不继承历史，模板强制自包含 |
| 多端按职责组织、联合验收 | `parallel-dev` §6–7 | 代理团队、共享任务列表、SendMessage |
| 跨仓逐仓核对规则与版本 | `parallel-dev` §7 | `--add-dir`、多会话 |
| 临时文件隔离与清理 | `02-engineering` | 会话 scratchpad |
| 提交/推送/发布分别授权 | `04-git-delivery` | 权限模式 |
| Hook 只做低误报确定性阻断，不用关键词判完成 | `hooks/policy-guard.py`、`hooks-optional.md` | PreToolUse `deny`/`ask` |
| 项目模板短而具体，不要文档五件套 | `project-bootstrap` 技能 | 项目 CLAUDE.md、`.claude/rules/` paths |
| 安装默认预览、事务清单、可卸载 | `install.sh` | — |

## Claude 特有的新增

- **worktree 基线陷阱**：子代理 worktree 默认从远端默认分支创建，看不到本地检查点。安装器在用户设置写入 `worktree.baseRef: "head"`（项目可覆盖），派发前提交契约检查点并用 `integrate.py preflight` 确认；`.worktreeinclude` 带入 gitignored 环境文件。
- **集成协议**：worktree 子代理在自己分支提交并回报 SHA，主会话检查越界 diff 后按依赖顺序合入，再在集成状态上验证。
- **只读角色的硬约束**：复核类子代理用 `tools` + `disallowedTools` 保证不能写文件，而不是只靠指令。
- **代理团队质量门**：`TaskCompleted` hook 以 exit 2 拒绝未通过验收的任务完成。
- **并行硬规则由 hook 强制**：派发 worktree 子代理前检查未提交改动与 baseRef；`implementer` 的 frontmatter hooks 强制归属声明、拦截越界编辑，并在结束时按分支创建点（reflog）核对提交与改动范围——后者也能发现经 Bash 写入的越界文件。
- **实测得出的约束**（2026-10-08 端到端验证）：Claude 的 worktree 隔离会拒绝子代理含 `$(...)` 的命令和指向 `.git/worktrees/` 的路径，因此归属声明改为"Write 一个约定文件 → hook 进程截获并存入私有 git 目录"；子代理定义在会话启动时缓存，修改 frontmatter hooks 后需新会话生效；以声明的基线做 diff 会把父分支后续提交误判为越界，故以分支创建点为准。
- **`ask` 决策**：Claude 的 PreToolUse 支持 `ask`，危险但可能合理的 git 操作交由用户确认，而不是一律拒绝。
- **上下文经济**：大范围检索交给 Explore；长任务用后台运行与完成通知，禁止轮询。

## 刻意舍弃

- Codex 专属：TOML 代理、Luna/Terra 模型配置、Codex 原生 Goal（Claude 侧改用 `/goal`，由用户发起）、`AGENTS.override.md`、`$skill` 调用语法、`apply_patch` 守卫、客户端兼容文档。
- 与内置能力重复的角色：`explorer-lite`、`planner`（内置 Explore/Plan）、`summarizer-lite`、`refiner`、`pr-preparer`、`security-lite`、`orchestrator`（主会话即编排者）。
- 审计目录、设计提示词技能（与开发规范无关）。
- 每次提交的完整 pre-push 历史扫描：改为 PreToolUse 在 `git commit` 时扫描暂存新增行；需要服务端级别保护时用仓库自己的 CI/secret scanning。
- 固定覆盖率门槛（如 80%）、强制 TDD、"写代码前必须 GitHub 搜索"：改为按行为与风险选择验证，复用优先但不设仪式。

## 维护约定

- 新增常驻规则必须对应具体故障，有适用范围和可观察的遵守方式；细节放技能，只维护一处。
- 改动后运行 `bash scripts/validate.sh`；它不会触碰真实的 `~/.claude`。
- 新增 hook 阻断须附带正例、反例（合法参数、带空格路径、引号中的文本）测试。

## 2026-10-08 升级的规则依据

| 规则 | 依据 |
| --- | --- |
| 任务分级 S/M/L（`02-engineering`） | 并行流程门槛不清导致实际从未触发（自审）；分级让流程重量与规模对应，M/L 以"契约是否变化、能否拆出 ≥2 个可独立验证的包"区分 |
| `/dev-spec-implement`、`/dev-spec-review` workflow | 官方 dynamic workflows：编排写在脚本里可复用、可续跑，`agent()` 支持 `agentType`/`isolation`/`schema`，结构化回报消除手写提示与自由格式回报的误差；对抗验证压低复核误报 |
| `integrate.py` 与"合并只走脚本" | 事件"集成环节缺少确定性步骤"；复核发现的越界、重命名、移除 worktree 后漏检均由脚本与测试固定 |
| 声明归属时校验基线 + `preflight` | 复核发现 workflow 内 `agent()` 可能绕过主会话的 Agent 钩子；改为在 worktree 侧和派发前两处确定性校验 |
| `# Compact instructions` 与 `integrate.py status` | 预防性：长任务压缩后最易丢失归属与证据（源自 codex-config 的续接条款）；状态优先从 git 恢复而非依赖摘要 |
| PR 与 CI（`dev-workflow` §7） | 规范此前止于本地提交（自审）；PR Steward 标签检查来自 CLI 内置约定，避免与其他代理争抢同一 PR |
| 从失误中学习（`01-core`） | 本会话的失误只修当下、未沉淀（自审）；新增规则须有依据以防膨胀 |
| 自我更新（`hooks/dev_spec_update.py`） | 用户要求"新规范推送后自主静默更新"。因为更新内容会以 hook 形式在每台设备执行，设计为"先在隔离 checkout 跑完整校验、再 fast-forward、失败回退"，且永不覆盖本地未提交/未推送的工作；一次性迁移（退役旧规则）不随更新重复 |

