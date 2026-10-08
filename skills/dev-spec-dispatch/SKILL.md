---
name: dev-spec-dispatch
description: 并行派发一条命令：校验工作包计划（归属重叠、缺字段）、提交契约检查点、运行 preflight、生成 /dev-spec-implement 的 args；集成后按改动规模与风险生成 /dev-spec-review 的 args。
when_to_use: L 档任务已定好契约与工作包、准备并行实现时；或并行集成后需要决定复核视角与 effort 时。
argument-hint: "[check|prepare|review-args] <计划文件或 --range>"
---

# 并行派发

脚本：`python3 ${CLAUDE_SKILL_DIR}/scripts/dispatch.py <check|prepare|review-args> …`。派发原则、hook 强制、集成与接管见 `parallel-dev` 技能，本技能只把准备步骤收成命令。

## 1. 写计划

计划文件放会话 scratchpad，不进仓库。两种格式等价。

Markdown（`负责`/`禁止`/`验收` 多项用 `;` 或 `，` 分隔，空单元格写 `-`）：

```markdown
契约: src/types/order.ts#Order

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

可选字段 `notes`（关键约束）；`effort` 取 low/medium/high/xhigh/max，核心逻辑包不填（继承会话）。

## 2. 校验

`dispatch.py check <计划>`（`--json` 结构化输出）。错误（退出码 2）：包名重复、缺 goal/owned/verify、effort 非法、两包 `owned` 重叠（与 workflow 同一判定：字面目录前缀相等或互为前缀即重叠）。某包 `owned` 落在另一包 `forbidden` 里只提示，确认是有意分工即可。

## 3. 准备

`dispatch.py prepare <计划> --commit --out <scratchpad>/args.json`

1. 再次校验计划；
2. `--commit`：工作区有改动时 `git add -A` 并提交检查点（默认消息 `chore: 并行派发检查点`，`--message` 可改）。在 main/master 上拒绝提交，先建任务分支；确需提交到默认分支才加 `--allow-default-branch`；
3. 运行 `parallel-dev` 的 `integrate.py preflight --base HEAD`，有阻塞则原样列出并退出 2，不生成 args；
4. 输出 args：`base` 为当前 HEAD 完整 SHA，`contract` 与 `packages` 透传（Markdown 转为同构 JSON）。

## 4. 派发

用 Workflow 工具运行 `dev-spec-implement`，传入 `args.json` 的内容。Workflow 不可用时按 `parallel-dev` 的手动派发（同一消息内并发多个 `implementer`，`isolation: "worktree"`），工作包内容取自同一份 args。

## 5. 集成后复核

按 `parallel-dev` 用 `integrate.py plan/apply` 合入后：

`dispatch.py review-args --range <检查点>..HEAD`

输出 `/dev-spec-review` 的 args：`range`、`lenses`、`finder_effort`，并附 `reasons`（每个视角的入选理由）与 `stats`。规则：

- 始终含 `correctness`、`tests`；
- 改动涉及类型/接口/schema/api/proto/openapi 目录或文件，或跨 ≥3 个顶层目录 → `contract`；
- 路径命中 auth、login、session、token、secret、credential、crypto、permission、acl、payment、upload、sql、query、exec、shell、hook、.env → `security`；
- 改动 >300 行，或路径命中 db、query、cache、index、batch、worker、perf → `performance`；
- 总改动 <400 行 `finder_effort` 为 `medium`，否则 `high`。

理由不成立时可手动增删视角；只处理复核结果中的 `confirmed`。
