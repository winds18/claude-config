---
name: dev-spec-dispatch
description: 并行派发的准备命令：校验工作包计划（归属重叠、缺字段）、提交契约检查点、运行 preflight，并生成每个包的用例设计 / 实现 / 包级复核提示；集成后按改动规模与风险选出复核视角并生成提示。
when_to_use: L 档任务已定好契约与工作包、准备并行实现时；或并行集成后需要决定复核视角与 effort 时。
argument-hint: "[env|check|prepare|prompts|review-args] <计划文件、args 文件或 --range>"
---

# 并行派发

脚本：`python3 ${CLAUDE_SKILL_DIR}/scripts/dispatch.py <env|check|prepare|prompts|review-args> …`。派发原则、hook 强制、集成与接管见 `parallel-dev` 技能，本技能只把准备步骤收成命令。

## 1. 看环境

`dispatch.py env`（`--json`）：列出本机的交付与隔离条件——是否显式设置了 git 身份、`gh` 是否可用、有无 node / docker、是否 SSH 会话、已在监听的端口。分配各包的端口前先看它；没有 `gh` 的机器（常见于服务器）止于提交与推送。

## 2. 写计划

计划文件放会话 scratchpad，不进仓库。两种格式等价。

Markdown（`负责`/`禁止`/`验收` 多项用 `;`、`,` 或 `，` 分隔；含分隔符或 `|` 的命令整条放进反引号，或把 `|` 写成 `\|`；空单元格写 `-`；每行列数必须与表头一致）：

```markdown
契约: src/types/order.ts#Order
用例设计: 是

| 包名 | 目标 | 负责 | 禁止 | 验收 | 准备 | 资源 | effort |
| --- | --- | --- | --- | --- | --- | --- | --- |
| api | 实现订单查询接口 | src/api/orders/**; tests/api/orders/** | src/types/**; package-lock.json | pnpm test tests/api/orders | pnpm install --frozen-lockfile | 端口 4101，数据库 app_test_api | medium |
| web | 订单列表页 | src/web/orders/** | src/types/** | pnpm test tests/web/orders | - | 端口 4102 | - |
```

JSON（与 `/dev-spec-implement` args 同构，`base` 可省略，由 `prepare` 填入）：

```json
{
  "contract": "src/types/order.ts#Order",
  "packages": [
    {"name": "api", "goal": "实现订单查询接口", "owned": ["src/api/orders/**", "tests/api/orders/**"],
     "forbidden": ["src/types/**", "package-lock.json"], "setup": "pnpm install --frozen-lockfile",
     "verify": ["pnpm test tests/api/orders"], "resources": "端口 4101，数据库 app_test_api", "effort": "medium"}
  ]
}
```

可选字段 `notes`（关键约束）；`effort` 取 low/medium/high/xhigh/max、`model`（Markdown 列名"模型"）取 sonnet/haiku/opus/fable/inherit 或完整模型 ID，核心逻辑包都不填（继承会话）。顶层可选 `case_design`（Markdown 写 `用例设计: 否`）：关闭对抗用例阶段，仅限纯机械改动。其他未知顶层字段会报错，避免静默丢失。

## 3. 校验

`dispatch.py check <计划>`（`--json` 结构化输出）。错误（退出码 2）：包名重复、缺 goal/owned/verify、effort 非法、两包 `owned` 重叠（与 workflow 同一判定：字面目录前缀相等或互为前缀即重叠）。某包 `owned` 落在另一包 `forbidden` 里只提示，确认是有意分工即可。

## 4. 准备

`dispatch.py prepare <计划> --commit --out <scratchpad>/args.json`

1. 再次校验计划；
2. `--commit`：工作区有改动时 `git add -A` 并提交检查点，并列出提交了哪些文件。先 `git status` 确认工作区里只有契约相关的改动（`add -A` 会带上一切未忽略的文件），并用 `--message` 写出这次契约变了什么（默认消息 `chore: 并行派发检查点` 只适合没有实质内容的检查点）。在 main/master 上拒绝提交，先建任务分支；确需提交到默认分支才加 `--allow-default-branch`。提交在脚本内进行、守卫看不到，所以脚本自己用守卫的检测器扫描暂存内容：疑似密钥、找不到守卫或提交失败（如 pre-commit 拒绝）时都不提交并恢复原暂存区；`--skip-secret-scan` 仅在确认安全后使用；
3. 运行 `parallel-dev` 的 `integrate.py preflight --base HEAD`，有阻塞则原样列出并退出 2，不生成 args；
4. 输出 args：`base` 为当前 HEAD 完整 SHA，`contract`、`packages`、`case_design` 透传（Markdown 转为同构 JSON）。

## 5. 派发

`dispatch.py prompts <args.json>`（`--package <包名>` 只看一个，`--out-dir <目录>` 每包写成一个文件）：输出每个包的三段提示——`case-designer` 的用例设计、`implementer` 的工作包（含归属声明、资源、验收与回报格式，并给出 Agent 调用参数）、`reviewer` 的包级复核。按 `parallel-dev` §2 的三阶段依次派发；机械性的包（`effort: low`）或 `用例设计: 否` 时自动跳过用例阶段。

Workflow 工具可用时，也可以把 `args.json` 直接交给 `dev-spec-implement`，两条路径提示里的关键要求由测试保证逐字一致。

## 6. 集成后复核

按 `parallel-dev` 用 `integrate.py plan/apply` 合入后：

`dispatch.py review-args --range <检查点>..HEAD --prompts`

输出每个入选视角的发现提示（标明用 `reviewer` 还是 `security-reviewer`、effort 与入选理由）和一段对抗验证提示。不加 `--prompts` 时输出 JSON：`range`、`lenses`、`finder_effort`，并附 `reasons`、`notes` 与 `stats`，可直接作为 `/dev-spec-review` 的 args。规则（路径按词匹配，不做子串匹配）：

- 始终含 `correctness`、`tests`；
- 目录名或文件名（去扩展名）是 types/interfaces/schema/api/proto/openapi/contract 等，或 `.proto/.d.ts/.graphql`，或跨 ≥3 个顶层目录 → `contract`；
- 路径词命中 auth/login/session/token/secret/credential/crypto/permission/acl/oauth/jwt/password/payment/upload/sql 等，或 `.env*` 文件，**或新增代码命中鉴权、凭证、命令执行、反序列化、请求输入等关键词** → `security`；未选时 `notes` 会提醒人工确认；
- 改动 >300 行，或路径词命中 db/query/cache/batch/worker/perf/migration（`index.*` 入口文件不算）→ `performance`；
- 总改动 <400 行 `finder_effort` 为 `medium`，否则 `high`。

规则是启发式：理由不成立或 `notes` 提示的情况存在时手动增删视角；只处理复核结果中的 `confirmed`。
