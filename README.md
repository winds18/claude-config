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
  dev-spec-dispatch/        /dev-spec-dispatch：计划表校验 → 检查点 → preflight → workflow 参数；复核规模计算
    scripts/dispatch.py     check / prepare / review-args
  project-bootstrap/        /project-bootstrap：项目 CLAUDE.md、settings、worktree 准备（含模板）
workflows/                  dynamic workflow，安装为 /命令
  dev-spec-implement.js     每包：对抗用例设计 → worktree implementer（先写测试）→ 包级复核
  dev-spec-review.js        多视角并行发现 → 去重 → 逐条对抗验证
agents/                     子代理：implementer、case-designer、reviewer、security-reviewer、test-triager
hooks/
  policy-guard.py           PreToolUse：高危删除、危险 git、绕过权限、密钥提交、worktree 派发检查
  dev_spec_update.py        SessionStart（异步）：校验后静默自我更新
  worktree-guard.py         implementer 专用：归属声明（含基线校验）、越界拦截、结束前提交核验
  hooks.json                守卫的 settings 片段
scripts/                    安装器（link/copy/doctor/uninstall）、validate.sh 与各项测试
  release.py                发布闸门：CI 通过的 main 提交 → tag + GitHub Release
.github/workflows/ci.yml    CI：Linux（Python 3.9 / 3.13）与 macOS 上跑全量校验
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
| `--no-hooks` / `--hooks` | 关闭 / 重新开启 PreToolUse 守卫（选择会被记住） |
| `--no-auto-update` | 关闭自动更新（见下文） |
| `--claude-home <dir>` | 安装到其他配置目录 |

- 所有被替换或退役的内容移入 `~/.claude/dev-spec-backups/<时间>/`，并记入 `~/.claude/.dev-spec-manifest.json`。
- `settings.json` 只增删 dev-spec 管理的两条 hook（守卫、自动更新）与缺省的 `worktree.baseRef`，其他内容保留。
- 不带 `--link`/`--copy` 重装时沿用上次的模式与选项。
- 守卫命令在脚本缺失时放行（exit 0）：软链接模式下外接盘未挂载不会阻断所有工具调用，但此时规范整体失效，`doctor` 会报告。

```bash
bash install.sh doctor
```

```bash
bash install.sh uninstall --apply
```

`doctor` 检查 CLI 版本、规范源是否可达、软链接是否失效、复制件是否落后于源、守卫条目是否在位。卸载按清单删除组件、恢复被替换的文件与旧规则、移除守卫条目；备份目录保留供人工确认。

## 自动更新

安装后默认开启。每次会话启动时，一个异步 `SessionStart` hook 在后台检查更新（默认最多每 6 小时一次），不拖慢启动、不打扰对话：

1. `git fetch` 安装时记录的规范仓库。默认 **stable 通道只跟随发布 tag（vX.Y.Z）**，合入 `main` 不会推到其他设备；`--channel main` 改为跟随主分支（只建议用于开发机）。已发布的 tag 被移动时拒绝跟随；
2. 只接受**干净的 fast-forward**：仓库有未提交改动、有未推送的本地提交、历史分叉或不在跟踪分支上时一律跳过（不会碰你正在开发的内容）；
3. 把新版本检出到临时 worktree，跑完整的 `scripts/validate.sh`，**不通过就保持当前版本**；
4. 通过后 fast-forward 并按安装时记住的选项重装；重装失败则把源仓库退回原版本。

| 命令 / 选项 | 作用 |
| --- | --- |
| `bash install.sh update` | 立即检查并更新，输出过程 |
| `bash install.sh doctor` | 查看最近一次检查的时间、结果与版本 |
| `--no-auto-update` / `--auto-update` | 关闭 / 重新开启（选择会被记住） |
| `--channel stable` / `--channel main` | 更新通道：发布 tag（默认）/ 主分支 |
| `--require-signed` | 只接受带有效签名的 tag（stable）或提交（main） |
| `DEV_SPEC_UPDATE_INTERVAL_HOURS` | 环境变量，覆盖检查间隔 |

结果写入 `~/.claude/dev-spec-update.json`，过程追加到 `~/.claude/dev-spec-update.log`。新规则从下一个会话起生效，技能、hook、workflow 即时生效。退役旧规则这类一次性迁移不会在更新时重复执行。

安全提示：自动更新会在每台设备上执行仓库里的 hook 代码，等同于信任该仓库的所有推送者。建议给 `main` 开启分支保护；对安全要求高的设备使用 `--require-signed`。

## 在项目中使用

1. 新项目或首次并行前运行 `/project-bootstrap`：项目 CLAUDE.md（只含验证过的命令）、`.claude/settings.json`、`.worktreeinclude`、gitignore 条目。
2. 开工先定档（`02-engineering`）：S 直接改；M 串行 + `/code-review`；L 走下面的流程；批量机械改动用 `/batch`。
3. L 档典型流程：

| 步骤 | 做法 |
| --- | --- |
| 定方案 | `dev-workflow` 需求→架构→契约；方向不明且代价高时才进 Plan 模式 |
| 准备 | `/dev-spec-dispatch`：计划表 → 校验归属 → 任务分支上提交检查点 → preflight → workflow 参数 |
| 并行实现 | `/dev-spec-implement`：每包先列对抗用例、先写测试后实现，得到 `ready` 分支与包级复核 |
| 集成 | `integrate.py plan` → `apply <分支…> --verify "<验收>"` → `cleanup` |
| 复核 | `dispatch.py review-args` 按规模与风险算出视角和 effort → `/dev-spec-review`，只处理被证实的问题 |
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

迭代时用 `bash scripts/validate.sh --changed` 只跑受改动影响的测试组；提交前与自动更新时跑全量。

发布：改动经 PR 合入 `main`、CI 通过后运行 `python3 scripts/release.py X.Y.Z`（先加 `--dry-run` 预览）。它只在 HEAD 等于 origin/main 且该提交的 CI 全部通过时打 tag，并从提交信息生成发布说明。

校验包括：子代理/技能 frontmatter（严格 YAML）、常驻规则行数预算、Markdown 相对链接、JSON/Python 语法、仓库内无疑似密钥；交叉引用（文档里的 integrate/install 子命令与参数、workflow 与技能名、§ 章节、子代理类型、workflow 参数字段、README 目录树都必须真实存在）；派发脚本测试；守卫行为测试；在真实 git worktree 上测试并行守卫与 `integrate.py`；用模拟运行时测试两个 workflow（含"运行时只传上一阶段结果"的严格变体）；用 bare origin + 源仓库 + 复制/软链接两种安装端到端测试自我更新（节流、持锁、校验失败拒绝、脏仓库与未推送跳过、安装失败回退、签名要求、关闭后保持）；在临时目录分别用复制与软链接模式完成安装往返（不触碰真实 `~/.claude`）。

修改原则：常驻规则只放跨任务硬原则，流程细节进技能且只维护一处；新增约束要在 `docs/incidents.md` 或 `docs/design.md` 中有依据。
