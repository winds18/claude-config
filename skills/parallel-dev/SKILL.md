---
name: parallel-dev
description: 并行开发执行手册：派发前检查、三阶段派发（用例设计、实现、包级复核）、integrate.py 集成与集成后复核、改派接管、桌面端多会话、跨仓联合验收与收尾。
when_to_use: L 档任务准备并行实现或多视角复核，需要合并多个 worktree 分支、恢复中断的并行状态、接管他人工作或协调多仓库交付时。
---

# 并行开发手册

前提：架构边界与共享契约已按 `dev-workflow` 确定。本手册只管"怎么安全地并行并收拢"。机制选择见常驻规则 `03-parallel`。

集成脚本：`python3 ${CLAUDE_SKILL_DIR}/scripts/integrate.py <preflight|status|plan|apply|cleanup> [参数] [--json]`，例如 `integrate.py status --json`。不带分支参数时，`plan` 只看 dev-spec 的 worktree（有归属声明或位于 `.claude/worktrees/` 下），`cleanup` 只清理有归属声明的；用户自己的 worktree 与并行会话必须显式点名才会处理。

## 1. 派发前检查（全部满足再派写入型工作）

1. **契约检查点**：共享类型/schema/接口已写好并在任务分支提交（本地检查点提交属于并行流程）。契约未变时，检查点就是派发时已提交的 HEAD。用户禁止提交时不并行，改串行。
2. **运行 `integrate.py preflight --base <检查点>`**：确认主工作树干净、检查点在 HEAD 历史中、`worktree.baseRef` 实际解析为 `"head"`（安装器在用户设置写入；项目设置可覆盖）。有阻塞不派发。
3. **环境**：worktree 需要的 gitignored 文件写进 `.worktreeinclude`；每个包写明依赖安装命令（`setup`）。各包的 `setup` 与验收命令应在项目 `permissions.allow` 里，否则后台子代理会卡在审批上。
4. **运行资源**：给每个写入者分配独立端口、数据库名、缓存与输出目录；无法隔离的步骤串行。先用 `/dev-spec-dispatch` 的 `env` 看本机已占用的端口（服务器上尤其要看）。
5. **归属**：各包 `owned` 互不重叠；共享文件（锁文件、全局配置、迁移、公共类型）放进 `forbidden`，归主会话。

## 2. 派发

包数 ≥ 2 时并行；只有 1 个包时主会话直接实现。

用 `/dev-spec-dispatch` 技能：写一张计划表，`prepare` 校验归属重叠、在任务分支提交检查点、运行 preflight 并输出 args；`prompts` 再按这份 args 生成每个包的三段提示。提示内容只维护在那个脚本里，不要手抄。

**三阶段**（桌面端会话的常规路径）：

1. **用例设计**：非机械性的包各派一个 `case-designer`（只读，同一消息内并发），拿到对抗用例。`effort: "low"` 的包与 `用例设计: 否` 的计划跳过。
2. **实现**：同一消息内并发多个 `Agent(subagent_type: "implementer", isolation: "worktree", run_in_background: true)`，把用例粘进提示的"先写成测试的场景"；给每个代理起名（提示里已给出 `impl-<包名>`）以便 `SendMessage` 续接。机械性的包在计划里设 `effort: "low"/"medium"`、`model: "sonnet"` 省成本，核心逻辑不设（继承会话）。
3. **包级复核**：每个回报 `STATUS: done` 的包派一个 `reviewer`，对照契约与用例复核。`fix-needed` 用 `SendMessage` 交回原实现者修复后复审，一轮为止；仍有分歧由主会话读代码裁决。

可集成的是 `done` 且复核 `pass` 的分支；`partial`、`blocked`、`fix-needed` 由主会话处理。验收命令尽量是可直接执行的入口（`./x.sh`、`make test`），避免被 worktree 隔离拒绝。

分多波派发（后一波依赖前一波的产出）时：前一波集成并验收后，在新的集成 HEAD 上重新 `prepare`，后一波以它为基线。

Workflow 工具可用时，可以把同一份 args 交给 `/dev-spec-implement`，它自动跑完上述三阶段并返回 `ready` / `needs_attention`。只派单个包、不经过脚本时，按 [references/work-package.md](references/work-package.md) 手写。

### Hook 强制

| 时机 | 检查 | 结果 |
| --- | --- | --- |
| 主会话用 Agent 工具派发 `isolation: "worktree"` | 主工作树有未提交改动；或 baseRef 非 head 且 HEAD 领先远端默认分支 | ask（workflow 内的派发不一定经过此钩子，所以派发前必须跑 preflight） |
| `implementer` 声明归属 | worktree 起点不包含声明的基线（检查点不可见）；基线提交不存在 | deny，implementer 按 blocked 回报 |
| `implementer` 在 worktree 内编辑 | 未声明归属（Write `.dev-spec-owner.json`，hook 截获并锁定）；路径在 forbidden 或不在 owned；改写已锁定声明 | deny |
| `implementer` 结束 | 未提交改动；自分支创建点无提交；改动越界（含 Bash 写入） | 阻止一次，第二次放行由回报说明 |

## 3. 进行中

- 主会话继续做未委派的关键路径工作，用完成通知回收结果，不轮询、不重复实现。
- 契约问题回报给契约负责人（通常主会话）：改契约 → 提交新检查点 → `SendMessage` 通知受影响代理新 SHA，由其合入后继续。
- 需求或约束变化：只更新受影响的工作包。

## 4. 集成（只走 integrate.py）

1. `integrate.py plan [分支…]`：核对每个分支的归属声明与真实 diff、跨分支重叠、worktree 未提交改动、主工作树是否干净。有阻塞就先处理（越界退回负责人；重叠说明归属被破坏）。
2. `integrate.py apply [分支…] --verify "<集成验收命令>"`：按给定顺序 `merge --no-ff`，冲突时自动 `merge --abort` 并停止，最后在集成状态上运行验收。顺序按依赖：被依赖的先合。
   - **冲突**：归属不重叠时冲突通常来自共享文件被多方改动，说明归属被破坏或契约在途变化。不要在主工作树手工拼接：把冲突分支交还其负责人（`SendMessage` 续接原 implementer，或派新的 implementer 接手该分支），让它在自己的 worktree 合入当前集成 HEAD 并解决冲突、重新提交，然后重新 `plan`。锁文件、生成文件这类归主会话的共享文件，由主会话在合并后统一重新生成并单独提交。
   - **验收失败**：交 `test-triager` 归因，修复归属方负责；修复后重跑 `apply` 的验收命令。
3. 集成后复核：`dispatch.py review-args --range <检查点>..HEAD --prompts` 按规模与风险选出视角，并生成每个视角的发现提示与对抗验证提示。同一消息内并发派出各视角的发现者，合并重复项后为每条候选派一个验证者；只处理 `confirmed`，`uncertain` 自行核实。Workflow 工具可用时，去掉 `--prompts` 得到的 JSON 就是 `/dev-spec-review` 的 args。
4. `integrate.py cleanup`：只移除已合入且干净的 worktree 与分支，其余列出原因。

压缩或恢复会话后先跑 `integrate.py status`，从 git 恢复各 worktree 的分支、归属、领先提交与合入状态。查看 worktree 用 `git -C <path>`，不要 `cd` 进去（会改变主会话的工作目录）。

## 5. 改派与接管

- 先确认原执行者已停止：完成/失败通知，或已 `TaskStop`。超时、失联、"已发停止请求"都不算停止。
- 用 `integrate.py status` 与 `git -C <worktree> status` 核对遗留状态，再把剩余工作连同现状交给新负责人；不双写。

## 6. 桌面端多会话

用于各自要长时间推进、可独立交付的大块工作（一个会话的上下文装不下，或需要各自的 PR）。短平快的包仍用子代理。

- 新会话勾选 **worktree**（或由任务 chip 启动），一会话一分支一 PR；契约先在基线分支提交，各会话从它出发。
- 主会话可以列出、读取其他桌面会话并给它们发消息：契约变化只发新事实、影响与新基线 SHA；对方忙时消息会排队。看不到终端 CLI 与云端会话。
- 每个会话的 PR 由桌面端状态栏监控 CI；Auto-fix 由用户开启，auto-merge 只在用户明确要求时开启。
- 合并顺序与联合验收仍由一个会话负责（见 §7）；归档其他会话需要用户同意。
- 代理团队（agent teams）只在终端 CLI 可用，桌面端不可用，本规范不依赖它。

## 7. 跨仓 / 多会话协调

仅当交付确实依赖多个仓库或既有会话时使用。

- 逐仓读取各自的 CLAUDE.md 与规则；Git 命令显式指定目录（`git -C`）。
- 记录每个仓库的分支与被测 SHA（或包版本）、契约权威来源、生产者/消费者关系；不能只约定"各仓最新版"。
- 指定一名联合验收负责人；证据绑定参与版本组合与实际环境，单仓 CI 绿不等于整体交付。
- 跨会话消息（SendMessage 到其他会话）会驱动对方执行：只发新事实、影响、基线和所需动作。

## 8. 收尾

- 在服务器（SSH 会话）上做完、要回本机继续时：推送任务分支，本机 `git fetch` 后从该分支接着做；不靠复制文件。
- 停止无关后台任务与代理；`integrate.py cleanup`，保留的 worktree 说明原因。
- 清理自建的临时目录、端口占用进程。
- 按 `04-git-delivery` 的完成条件与交付格式汇报，附 `integrate.py apply` 的验收结果。
