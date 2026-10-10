---
name: parallel-dev
description: 并行开发执行手册：派发前检查、工作包、/dev-spec-implement 与 /dev-spec-review workflow、integrate.py 集成、改派接管、代理团队、跨仓联合验收与收尾。
when_to_use: L 档任务准备并行实现或多视角复核，需要合并多个 worktree 分支、恢复中断的并行状态、接管他人工作或协调多仓库交付时。
---

# 并行开发手册

前提：架构边界与共享契约已按 `dev-workflow` 确定。本手册只管"怎么安全地并行并收拢"。机制选择见常驻规则 `03-parallel`。

集成脚本：`python3 ${CLAUDE_SKILL_DIR}/scripts/integrate.py <preflight|status|plan|apply|cleanup> [参数] [--json]`，例如 `integrate.py status --json`。不带分支参数时，`plan` 只看 dev-spec 的 worktree（有归属声明或位于 `.claude/worktrees/` 下），`cleanup` 只清理有归属声明的；用户自己的 worktree 与并行会话必须显式点名才会处理。

## 1. 派发前检查（全部满足再派写入型工作）

1. **契约检查点**：共享类型/schema/接口已写好并在任务分支提交（本地检查点提交属于并行流程；推送仍需授权）。契约未变时，检查点就是派发时已提交的 HEAD。用户禁止提交时不并行，改串行。
2. **运行 `integrate.py preflight --base <检查点>`**：确认主工作树干净、检查点在 HEAD 历史中、`worktree.baseRef` 实际解析为 `"head"`（安装器在用户设置写入；项目设置可覆盖）。有阻塞不派发。
3. **环境**：worktree 需要的 gitignored 文件写进 `.worktreeinclude`；每个包写明依赖安装命令（`setup`）。
4. **运行资源**：给每个写入者分配独立端口、数据库名、缓存与输出目录；无法隔离的步骤串行。
5. **归属**：各包 `owned` 互不重叠；共享文件（锁文件、全局配置、迁移、公共类型）放进 `forbidden`，归主会话。

## 2. 派发

包数 ≥ 2 时并行；只有 1 个包时主会话直接实现。

**最省事的入口是 `/dev-spec-dispatch` 技能**：写一张计划表，它负责校验归属重叠、在任务分支提交检查点、运行 preflight，并输出下面这份 args。

**首选 `/dev-spec-implement` workflow**（Workflow 工具可用时）。它为每个包派一个 worktree 隔离的 `implementer`，强制结构化回报，随后由独立 `reviewer` 对照契约复核该包；参数无效（缺基线、归属重叠、缺验收）时直接报错，不派发。args 示例：

```json
{
  "base": "<检查点 SHA>",
  "contract": "src/types/order.ts#Order",
  "packages": [
    {"name": "api", "goal": "实现订单查询接口", "owned": ["src/api/orders/**", "tests/api/orders/**"],
     "forbidden": ["src/types/**", "package-lock.json"], "setup": "pnpm install --frozen-lockfile",
     "verify": ["pnpm test tests/api/orders"], "resources": "端口 4101，数据库 app_test_api", "effort": "medium"}
  ]
}
```

- 机械性的包设 `effort: "low"/"medium"` 省成本；核心逻辑不设（继承会话）。
- workflow 会先为每个包运行 `case-designer` 列出对抗用例，实现者先写成测试再实现，包级复核逐条核对。相称性：`effort: "low"` 的包自动跳过用例设计，`case_design: false` 全局关闭；验收命令尽量是可直接执行的入口（`./x.sh`、`make test`），避免被 worktree 隔离拒绝。
- 结果中 `ready` 是 done 且包级复核通过的分支，`needs_attention` 需要你处理（blocked、partial、fix-needed）。

**手动派发**（Workflow 工具不可用时——桌面端会话目前常见——按同样的三阶段进行）：
1. 非机械性的包先各派一个 `case-designer`（只读，可并发），拿到对抗用例；
2. 并发派 `implementer`，把用例写进工作包的"先写成测试的场景"；
3. 每个完成的包派一个 `reviewer` 做包级复核，fix-needed 的用 `SendMessage` 交回原 implementer 修复后复审。

工作包写法：按 [references/work-package.md](references/work-package.md) 写自包含工作包，同一消息内并发多个 `Agent(subagent_type: "implementer", isolation: "worktree", run_in_background: true)`，给每个代理起名以便 `SendMessage` 续接。

### Hook 强制

| 时机 | 检查 | 结果 |
| --- | --- | --- |
| 主会话用 Agent 工具派发 `isolation: "worktree"` | 主工作树有未提交改动；或 baseRef 非 head 且 HEAD 领先远端默认分支 | ask（workflow 内的 `agent()` 不一定经过此钩子，所以派发前必须跑 preflight） |
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
3. 集成后复核：`/dev-spec-review` workflow，args 用 `/dev-spec-dispatch` 的 `review-args --range <检查点>..HEAD` 生成（按规模与风险选视角和 effort）；只处理 `confirmed`，`uncertain` 自行核实。不可用时并行派 `reviewer` 与 `security-reviewer`。
4. `integrate.py cleanup`：只移除已合入且干净的 worktree 与分支，其余列出原因。

压缩或恢复会话后先跑 `integrate.py status`，从 git 恢复各 worktree 的分支、归属、领先提交与合入状态。查看 worktree 用 `git -C <path>`，不要 `cd` 进去（会改变主会话的工作目录）。

## 5. 改派与接管

- 先确认原执行者已停止：完成/失败通知，或已 `TaskStop`。超时、失联、"已发停止请求"都不算停止。
- 用 `integrate.py status` 与 `git -C <worktree> status` 核对遗留状态，再把剩余工作连同现状交给新负责人；不双写。

## 6. 代理团队要点

- 适合：多视角研究/评审、竞争性假设排障、前后端分端长期协作。不适合：顺序任务、同文件编辑、强依赖。
- 派生提示给足上下文（队友不继承主会话历史）；每人 5–6 个自包含任务；按文件组划分避免覆盖（团队不做 worktree 隔离）。
- 质量门：项目 hooks 用 `TaskCompleted`（exit 2 拒绝完成）运行该任务的验收命令，模板见 project-bootstrap 的 `templates/hooks-optional.md`。
- 限制：`/resume` 不恢复进程内队友；一个会话一个团队；队友不能再建团队。成本约为单会话数倍，队友优先用 sonnet。

## 7. 跨仓 / 多会话协调

仅当交付确实依赖多个仓库或既有会话时使用。

- 逐仓读取各自的 CLAUDE.md 与规则；Git 命令显式指定目录（`git -C`）。
- 记录每个仓库的分支与被测 SHA（或包版本）、契约权威来源、生产者/消费者关系；不能只约定"各仓最新版"。
- 指定一名联合验收负责人；证据绑定参与版本组合与实际环境，单仓 CI 绿不等于整体交付。
- 跨会话消息（SendMessage 到其他会话）会驱动对方执行：只发新事实、影响、基线和所需动作。

## 8. 收尾

- 停止无关后台任务与代理；`integrate.py cleanup`，保留的 worktree 说明原因。
- 清理自建的临时目录、端口占用进程。
- 按 `04-git-delivery` 的完成条件与交付格式汇报，附 `integrate.py apply` 的验收结果。
