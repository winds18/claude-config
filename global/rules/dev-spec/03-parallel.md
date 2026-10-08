<!-- dev-spec 并行约定：每个会话加载。派发、集成、冲突与接管的具体做法只维护在 skill parallel-dev。 -->
# 并行协作约定

## 选择机制（取最轻的可行项）

| 需要 | 机制 |
| --- | --- |
| 只要结论的大范围检索、长日志或失败归因 | `Explore` / `test-triager` 子代理 |
| 2 个以上可独立验证的包并行实现 | `parallel-dev` 技能：`/dev-spec-implement` workflow，不可用时手动派 `implementer` |
| L 档集成后的多视角复核 | `/dev-spec-review` workflow，不可用时并行派 `reviewer` / `security-reviewer` |
| 同一机械变换作用于大量文件 | `/batch` 或 workflow pipeline |
| 成员需互相讨论、长期分端协作 | 代理团队（实验特性，需启用） |

- 只有 1 个包或强依赖时不并行，主会话直接实现。委派后不重复调查同一内容，用完成通知回收结果，不轮询。

## 写入归属（硬规则；worktree 内由 hook 强制）

- 同一写入范围同一时段只有一个负责人：文件组、共享类型/schema、锁文件、全局配置、迁移，以及测试用的数据库/端口/缓存/输出目录。共享文件默认归主会话。
- 并行写代码一律 worktree 隔离，派发前把契约提交为检查点。用户禁止提交时不做并行写，改为串行。worktree 不隔离端口、数据库和外部服务，须另行分配。
- 代理团队不做 worktree 隔离、不受归属 hook 约束：只按文件组划分给不同队友，并在集成时核对 diff。
- 改派或接管前确认原执行者已停止写入（完成通知或 TaskStop）；失联、超时不等于已停止。

## 集成与恢复

- 派发前、合并、清理都用 `integrate.py`（parallel-dev 技能内，默认 `~/.claude/skills/parallel-dev/scripts/integrate.py`）的 `preflight` / `plan` / `apply --verify` / `cleanup`；冲突按该技能处理。不凭"已完成"摘要合入。
- 单包通过、mock 通过只是阶段证据；验收以集成状态上的检查为准。
- 压缩或恢复会话后先运行 `integrate.py status` 从 git 恢复并行状态，不靠记忆。
- 结束前停止无关后台任务和代理，清理已合并的 worktree；有意保留的说明用途。
