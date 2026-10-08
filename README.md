# dev-spec：Claude Code 开发规范

一套可移植的 Claude Code 个人开发规范，目标是**严谨且高效的并行开发**：主会话推进关键路径并负责集成，独立工作包交给隔离的子代理并行完成，以当前集成状态的真实证据验收。

设计取舍与对 [codex-config](https://github.com/winds18/codex-config) 的继承/舍弃见 [docs/design.md](docs/design.md)。

## 结构

```text
global/
  CLAUDE.md                 个人全局指令模板（仅目标机器没有时写入）
  rules/dev-spec/           常驻规则，每个会话加载（≤200 行）
    01-core.md              自主推进、上下文经济、授权与安全
    02-engineering.md       流程入口、质量底线、测试验证、临时文件
    03-parallel.md          何时并行、写入归属硬规则、交接与验收
    04-git-delivery.md      Git、完成条件、交付说明格式
skills/                     按需加载的流程
  dev-workflow/             需求→架构→契约→并行实现→集成；质量表、测试矩阵
  parallel-dev/             机制选择、派发前检查、工作包、集成、接管、代理团队、跨仓
  project-bootstrap/        /project-bootstrap：项目 CLAUDE.md、settings、worktree 准备（含模板）
agents/                     子代理
  implementer.md            文件组负责人（继承模型，并行时用 worktree 隔离）
  reviewer.md               只读复核（工具层禁止写入）
  security-reviewer.md      只读安全复核
  test-triager.md           只读失败归因（sonnet / medium）
hooks/
  policy-guard.py           PreToolUse 守卫：高危删除、危险 git、绕过权限、密钥提交、worktree 派发检查
  worktree-guard.py         implementer 专用：归属声明与越界拦截、结束前提交核验
  hooks.json                守卫的 settings 片段
scripts/                    安装器（link/copy/doctor/uninstall）、校验与测试
docs/design.md              设计说明
```

## 安装

需要 Bash、Python 3.9+，Claude Code ≥ 2.1.212（低于此版本时 worktree 隔离等字段会被静默忽略，安装器会拒绝；先 `claude update`）。默认只预览，不写任何文件。

**本机（单一真源，修改即时生效）**：软链接到本仓库，接管 CLAUDE.md，退役 `rules/` 下的旧规则。

```bash
bash install.sh --link --manage-claude-md --retire-legacy-rules --apply
```

**其他设备**：克隆本仓库后复制安装；之后 `git pull` 并重新执行同一命令即可更新。

```bash
git clone https://github.com/winds18/claude-config.git
```

```bash
cd claude-config && bash install.sh --copy --manage-claude-md --retire-legacy-rules --apply
```

| 选项 | 作用 |
| --- | --- |
| `--link` / `--copy` | 软链接到仓库，或复制（默认） |
| `--manage-claude-md` | 接管 `~/.claude/CLAUDE.md`，旧文件备份；不加则仅在不存在时写入 |
| `--retire-legacy-rules` | 把 `~/.claude/rules/` 下除 `dev-spec` 外的旧规则移入备份 |
| `--force` | 同名但非本规范管理的条目（如你自己的同名技能）备份后替换；默认跳过并提示 |
| `--no-hooks` | 不安装 / 移除 PreToolUse 守卫 |
| `--claude-home <dir>` | 安装到其他配置目录 |

- 所有被替换或退役的内容移入 `~/.claude/dev-spec-backups/<时间>/`，并记入 `~/.claude/.dev-spec-manifest.json`。
- `settings.json` 只增删 dev-spec 守卫这一条，其他内容保留。
- 守卫命令在脚本缺失时放行（exit 0）：软链接模式下外接盘未挂载不会阻断所有工具调用，但此时规范整体失效，`doctor` 会报告。

```bash
bash install.sh doctor
```

```bash
bash install.sh uninstall --apply
```

`doctor` 检查 CLI 版本、规范源是否可达、软链接是否失效、复制件是否落后于源、守卫条目是否在位。卸载按清单删除组件、恢复被替换的文件与旧规则、移除守卫条目；备份目录保留供人工确认。

## 在项目中使用

1. 新项目或首次并行前运行 `/project-bootstrap`：生成项目 CLAUDE.md（只含验证过的命令）、`.claude/settings.json`（含必需的 `worktree.baseRef: "head"`）、`.worktreeinclude`、gitignore 条目。
2. 日常小修：直接说需求，规则层自动生效。
3. 跨模块功能：Claude 按 `dev-workflow` 定架构与契约 → 提交契约检查点 → 按 `parallel-dev` 并行派发 `implementer`（各自 worktree）→ 合入并在集成状态验证 → `reviewer` 复核 → 按完成条件交付。
4. 多视角评审或竞争性假设排障：可在项目设置中启用代理团队（`CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1`），质量门 hook 见 `skills/project-bootstrap/templates/hooks-optional.md`。

## 守卫行为

| 决策 | 场景 |
| --- | --- |
| deny | `rm -r` 根目录/主目录/系统目录/`..`；会话内启动跳过权限的 `claude`；暂存内容含高置信度密钥（私钥、AWS、GitHub、Anthropic、OpenAI、Slack、Google、Stripe live） |
| ask | `rm -r .`；对受保护分支或未指定分支强推；删除远端分支；`reset --hard`、`clean -f`、`checkout/restore .`、`stash drop/clear`、`branch -D`、`worktree remove --force`；疑似硬编码凭证的提交；向文件写入疑似密钥 |

### 并行守卫

| 时机 | 检查 | 决策 |
| --- | --- | --- |
| 主会话派发 `isolation: "worktree"` 的 Agent | 主工作树有未提交改动；或 `worktree.baseRef` 非 head 且 HEAD 领先远端默认分支 | ask |
| `implementer` 在 worktree 内编辑 | 未声明归属；路径在 forbidden 或不在 owned；试图改写已锁定的声明 | deny |
| `implementer` 结束 | 未提交改动；自分支创建点无提交；改动越界（含经 Bash 写入） | 阻止一次结束 |

归属声明：子代理用 Write 工具写 worktree 根目录 `.dev-spec-owner.json`，hook 截获后存入 worktree 私有 git 目录并锁定，文件不进入工作区。安装器会在用户设置里写入 `worktree.baseRef: "head"`（已有值时不动）。

修改 `agents/*.md`（含 frontmatter hooks）后需**新开会话**才生效：子代理定义在会话启动时缓存。

守卫只解析直接命令，不理解解释器、eval、变量展开；内部异常时放行。它是补充检查，权限模式与沙箱才是边界。确认是假数据时在该行加注释 `dev-spec: allow-secret`。

## 维护

```bash
bash scripts/validate.sh
```

校验包括：子代理/技能 frontmatter 字段与取值、常驻规则行数预算、Markdown 相对链接、JSON/Python 语法、仓库内无疑似密钥、守卫行为测试，以及在临时目录分别用复制与软链接模式完成"预览→安装→冲突保护→接管与退役→幂等→守卫生效→脚本缺失放行→卸载完全还原"往返（不触碰真实 `~/.claude`）。

修改原则：常驻规则只放跨任务硬原则，流程细节进技能且只维护一处；新增约束要对应具体故障并可观察。
