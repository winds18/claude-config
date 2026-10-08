# 事件记录

规范的每一条新增或修改都应能追溯到这里的一条事件。格式：现象 → 原因 → 修订（落点）。只记录规范本身的缺陷；项目层面的教训写入该项目的自动记忆。

## 2026-10-08 worktree 子代理无法写归属声明

- 现象：端到端测试中 implementer 用 `cat > "$(git rev-parse --git-dir)/…"` 声明归属被拒，改用字面路径同样被拒。
- 原因：Claude 的 worktree 隔离拒绝子代理执行含 `$(...)` 的命令及指向 `.git/worktrees/` 的路径。
- 修订：改为 Write 约定文件 `.dev-spec-owner.json`，由 hook 进程截获并写入私有 git 目录、锁定（`hooks/worktree-guard.py`、`agents/implementer.md`）。

## 2026-10-08 frontmatter hooks 在当前会话不生效

- 现象：同一测试中，声明前的写入没有被拒绝；直接调用守卫脚本则返回 deny。
- 原因：子代理定义在会话启动时缓存，之后添加的 frontmatter hooks 不会进入已启动的会话。
- 修订：README 注明修改 `agents/*.md` 后需新开会话；端到端验证必须在新会话进行。

## 2026-10-08 结束核验误判越界

- 现象：worktree 从晚于声明基线的 HEAD 创建时，主会话之后的提交被算作子代理的越界改动。
- 原因：以声明的 base 做 diff，而不是分支自身的创建点。
- 修订：结束核验以分支 reflog 的创建点为准，base 仅作兜底（`hooks/worktree-guard.py`）；`integrate.py` 默认用 merge-base。

## 2026-10-08 会话工作目录被切进 worktree

- 现象：排查时 `cd <worktree> && …` 使主会话的工作目录变成测试 worktree，环境随之按 worktree 隔离处理。
- 原因：Bash 的工作目录在调用之间保持；主会话进入 worktree 会改变后续所有命令的上下文。
- 修订：查看 worktree 一律用 `git -C <path>` 或绝对路径，不 `cd` 进去（`skills/parallel-dev/SKILL.md` §4 末段）。

## 2026-10-08 脱敏后的测试假值触发占位符白名单

- 现象：把测试密码改为含 "Fake" 的值后，"疑似硬编码凭证应 ask" 的测试失败。
- 原因：守卫对含 example/fake/test 等词的值视为占位符放行。
- 修订：测试用随机无语义的假值并在运行时拼接；公开仓库推送前的脱敏检查纳入 `scripts/validate.sh`（仓库内无疑似密钥）。

## 2026-10-08 集成环节缺少确定性步骤

- 现象：派发与结束都有 hook 把关，但合并、越界核对、集成验收与清理全靠手工，压缩后难以恢复状态。
- 原因：流程只规定了"做什么"，没有提供确定性工具。
- 修订：新增 `skills/parallel-dev/scripts/integrate.py`（status/plan/apply/cleanup），规则要求合并只走该脚本、恢复先运行 status。

## 2026-10-08 复核发现集成脚本与 workflow 的漏洞

- 现象：独立 reviewer 在临时仓库复现：`cleanup` 默认会删掉用户自己的 worktree 甚至 `main` 分支；把 forbidden 文件重命名进 owned 不被发现；worktree 移除后越界检查整体失效；同名 tag 干扰分支解析；`./` 前缀与目录名中间的通配符让归属重叠检查漏报；代理抛错时复核发现与实现回报被静默丢弃；测试桩与脚本依赖同一个未经验证的运行时签名假设。
- 原因：默认目标范围过宽；`git diff --name-only` 默认开启改名检测；归属只从现存 worktree 读取；测试只覆盖了作者设想的路径（"两套同假设的 mock"）。
- 修订：`integrate.py` 默认只处理 dev-spec worktree、统一用 `refs/heads/`、`--no-renames`、按分支持久化归属副本、无声明即阻塞；守卫结束核验同样 `--no-renames`；workflow 不依赖 pipeline 第二参数、失败保留为 uncertain/错误并列出 `failed_lenses`；测试新增严格运行时变体与每条复现用例。

## 2026-10-08 规则复核发现指令冲突

- 现象：L 档无条件要求 Plan 模式（与自主推进冲突）；"禁止提交时改用主工作树并行"无法执行且无 hook 约束；包数阈值三处不一致；常驻规则重复技能细节；本轮新增规则缺少依据记录。
- 原因：同一流程在常驻规则与技能中各写一份，修改时只改了一处。
- 修订：Plan 模式改为有条件；删除主工作树并行回退（禁止提交即串行）；阈值统一为"≥2 个可独立验证的包"并只写在 `03-parallel`；常驻规则只保留原则与指针，细节归 `parallel-dev`/`dev-workflow`；依据补记于本文件与 `design.md`。

## 2026-10-08 修复核验发现的残余问题

- 现象：复核者核验修复时复现：分支删除后留下的持久化归属副本会被同名新分支继承，使用户自己的 worktree 被当作 dev-spec 管理并被 cleanup 删除；`.claude/worktrees/` 下未声明的 worktree（可能是用户的并行会话）会被默认 cleanup 删除；目录缺失时执行了全局 `worktree prune`；`failed_lenses` 用了过滤后数组的下标；preflight 只比较 origin/HEAD 与 HEAD。另：git 辅助函数 `.strip()` 截掉 porcelain 首行前导空格，导致首个改动路径少一个字符，守卫会把负责范围内的文件误判为越界（真实运行时发现）。
- 原因：持久化副本缺少与分支历史的绑定；"受管"判定混用了副本与路径；输出清洗过度。
- 修订：副本记录 `declared_at` 并要求其在分支历史中；"受管"只看私有声明或路径，默认 cleanup 只处理有声明的；目录缺失时跳过；按原始下标标注失败视角；preflight 检查检查点是否在 worktree 实际起点的历史中；git 输出只去尾部换行。每项均有回归测试，且已用"恢复旧代码 → 测试失败"确认测试有效。

