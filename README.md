# dev-spec：Claude Code 开发规范

一套可移植的 Claude Code 个人开发规范，目标是**严谨且高效的并行开发**：主会话推进关键路径并负责集成，独立工作包交给隔离的子代理并行完成，以当前集成状态的真实证据验收。

设计取舍与对 [codex-config](https://github.com/winds18/codex-config) 的继承/舍弃见 [docs/design.md](docs/design.md)。

## 结构

```text
global/
  CLAUDE.md                 个人全局指令：语言、长期并行授权、压缩时必须保留的信息
  rules/dev-spec/           常驻规则，每个会话加载（≤200 行，validate 强制）
    01-core.md              自主推进、上下文经济、授权与安全、从失误中学习
    02-engineering.md       任务分级 S/M/L、质量底线、测试验证、临时文件
    03-parallel.md          机制选择、写入归属硬规则、集成与恢复
    04-git-delivery.md      Git、PR 入口、完成条件、交付说明格式
skills/                     按需加载的流程（细节只维护在这里）
  dev-workflow/             L 档五阶段、质量表、测试矩阵、复核、PR 与 CI
  parallel-dev/             派发前检查、工作包、workflow 派发、集成与冲突、接管、代理团队、跨仓
    scripts/integrate.py    确定性集成：preflight / status / plan / apply / cleanup
  project-bootstrap/        /project-bootstrap：项目 CLAUDE.md、settings、worktree 准备（含模板）
workflows/                  dynamic workflow，安装为 /命令
  dev-spec-implement.js     每包一个 worktree implementer + 结构化回报 + 包级复核
  dev-spec-review.js        多视角并行发现 → 去重 → 逐条对抗验证
agents/                     子代理：implementer、reviewer、security-reviewer、test-triager
hooks/
  policy-guard.py           PreToolUse：高危删除、危险 git、绕过权限、密钥提交、worktree 派发检查
  worktree-guard.py         implementer 专用：归属声明（含基线校验）、越界拦截、结束前提交核验
  hooks.json                守卫的 settings 片段
scripts/                    安装器（link/copy/doctor/uninstall）、validate.sh 与各项测试
docs/
  design.md                 设计取舍与依据
  incidents.md              事件记录：每条规则修订的来源
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

1. 新项目或首次并行前运行 `/project-bootstrap`：项目 CLAUDE.md（只含验证过的命令）、`.claude/settings.json`、`.worktreeinclude`、gitignore 条目。
2. 开工先定档（`02-engineering`）：S 直接改；M 串行 + `/code-review`；L 走下面的流程；批量机械改动用 `/batch`。
3. L 档典型流程：

| 步骤 | 做法 |
| --- | --- |
| 定方案 | `dev-workflow` 需求→架构→契约；方向不明且代价高时才进 Plan 模式 |
| 检查点 | 提交契约，`integrate.py preflight --base <SHA>` 无阻塞 |
| 并行实现 | `/dev-spec-implement`（args：base、contract、packages），得到 `ready` 分支与包级复核 |
| 集成 | `integrate.py plan` → `apply <分支…> --verify "<验收>"` → `cleanup` |
| 复核 | `/dev-spec-review`（args：`{"range": "<检查点>..HEAD"}`），只处理被证实的问题 |
| 交付 | 完成条件 + 交付说明；PR/CI 见 `dev-workflow` §7 |

4. 会话被压缩或恢复后，先 `integrate.py status` 从 git 恢复并行状态。
5. 有可验证终态的长任务，可用 `/goal <验收条件>` 让独立评估器判定完成；多视角评审或竞争性假设排障可启用代理团队（实验特性）。

dynamic workflow 需在 `/config` 中开启（部分计划默认关闭）；不可用时规范自动回退为手动派发子代理。

## 守卫行为

| 决策 | 场景 |
| --- | --- |
| deny | `rm -r` 根目录/主目录/系统目录/`..`；会话内启动跳过权限的 `claude`；暂存内容含高置信度密钥（私钥、AWS、GitHub、Anthropic、OpenAI、Slack、Google、Stripe live） |
| ask | `rm -r .`；对受保护分支或未指定分支强推；删除远端分支；`reset --hard`、`clean -f`、`checkout/restore .`、`stash drop/clear`、`branch -D`、`worktree remove --force`；疑似硬编码凭证的提交；向文件写入疑似密钥 |

### 并行守卫

| 时机 | 检查 | 决策 |
| --- | --- | --- |
| 主会话用 Agent 工具派发 `isolation: "worktree"` | 主工作树有未提交改动；或 `worktree.baseRef` 非 head 且 HEAD 领先远端默认分支 | ask |
| `implementer` 声明归属 | worktree 起点不含声明的基线；基线不存在（覆盖 workflow 派发绕过上一行的情况） | deny → blocked |
| `implementer` 在 worktree 内编辑 | 未声明归属；路径在 forbidden 或不在 owned；试图改写已锁定的声明 | deny |
| `implementer` 结束 | 未提交改动；自分支创建点无提交；改动越界（含经 Bash 写入） | 阻止一次结束 |

归属声明：子代理用 Write 工具写 worktree 根目录 `.dev-spec-owner.json`，hook 截获后存入 worktree 私有 git 目录并锁定，同时按分支名在共享 git 目录留一份副本，worktree 删除后 `integrate.py` 仍能核对越界。改名按新旧两条路径计算。安装器会在用户设置里写入 `worktree.baseRef: "head"`（已有值时不动）。

修改 `agents/*.md`（含 frontmatter hooks）后需**新开会话**才生效：子代理定义在会话启动时缓存。

守卫只解析直接命令，不理解解释器、eval、变量展开；内部异常时放行。它是补充检查，权限模式与沙箱才是边界。确认是假数据时在该行加注释 `dev-spec: allow-secret`。

## 维护

```bash
bash scripts/validate.sh
```

校验包括：子代理/技能 frontmatter（严格 YAML）、常驻规则行数预算、Markdown 相对链接、JSON/Python 语法、仓库内无疑似密钥；守卫行为测试；在真实 git worktree 上测试并行守卫与 `integrate.py`；用模拟运行时测试两个 workflow（含"运行时只传上一阶段结果"的严格变体）；在临时目录分别用复制与软链接模式完成安装往返（不触碰真实 `~/.claude`）。

修改原则：常驻规则只放跨任务硬原则，流程细节进技能且只维护一处；新增约束要在 `docs/incidents.md` 或 `docs/design.md` 中有依据。
