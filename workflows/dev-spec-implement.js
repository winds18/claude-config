export const meta = {
  name: 'dev-spec-implement',
  description: 'dev-spec 并行实现：每包先由 case-designer 列出对抗用例，再由 worktree 隔离的 implementer 先写测试后实现并结构化回报，最后由独立 reviewer 对照契约与用例做包级复核',
  phases: ['用例设计', '实现', '包级复核'],
}

// args: {
//   base: "<契约检查点 SHA>",
//   contract: "<契约权威来源，如 src/types/api.ts#Order>",           // 可选
//   packages: [{
//     name, goal, owned: [glob], forbidden?: [glob], setup?: "<命令>",
//     verify: ["<验收命令>"], resources?: "<端口/数据库/输出目录>", notes?: "<关键约束>",
//     effort?: "low"|"medium"|"high"|"xhigh"|"max"
//   }],
//   case_design?: true    // false 时跳过用例设计阶段
// }
// 返回每个包的结构化回报与复核结论；合并由主会话用 integrate.py 完成（workflow 不能写主工作树）。

const REPORT = {
  type: 'object',
  required: ['status', 'branch', 'sha', 'files', 'checks', 'unverified', 'contract_issues'],
  properties: {
    status: { type: 'string', enum: ['done', 'partial', 'blocked'] },
    branch: { type: 'string' },
    sha: { type: 'string' },
    files: { type: 'array', items: { type: 'string' } },
    checks: {
      type: 'array',
      items: {
        type: 'object',
        required: ['command', 'exit_code', 'summary'],
        properties: { command: { type: 'string' }, exit_code: { type: 'integer' }, summary: { type: 'string' } },
      },
    },
    unverified: { type: 'array', items: { type: 'string' } },
    contract_issues: { type: 'array', items: { type: 'string' } },
    notes: { type: 'string' },
  },
}

const CASES = {
  type: 'object',
  required: ['cases'],
  properties: {
    cases: {
      type: 'array',
      items: {
        type: 'object',
        required: ['priority', 'name', 'given', 'when', 'then', 'prevents'],
        properties: {
          priority: { type: 'string', enum: ['high', 'medium', 'low'] },
          name: { type: 'string' },
          given: { type: 'string' },
          when: { type: 'string' },
          then: { type: 'string' },
          prevents: { type: 'string' },
        },
      },
    },
  },
}

const VERDICT = {
  type: 'object',
  required: ['verdict', 'findings'],
  properties: {
    verdict: { type: 'string', enum: ['pass', 'fix-needed'] },
    findings: {
      type: 'array',
      items: {
        type: 'object',
        required: ['severity', 'location', 'problem', 'trigger'],
        properties: {
          severity: { type: 'string', enum: ['critical', 'high', 'medium', 'low'] },
          location: { type: 'string' },
          problem: { type: 'string' },
          trigger: { type: 'string' },
        },
      },
    },
  },
}

// 字面目录前缀：规范化 ./ 与结尾 /，截到第一个通配符之前的最后一个 /（通配符在目录名中间时退回上一级）
function prefixOf(glob) {
  const p = String(glob).trim().replace(/^(\.\/)+/, '').replace(/\/+$/, '')
  const wild = p.search(/[*?[]/)
  if (wild < 0) return p
  const cut = p.lastIndexOf('/', wild)
  return cut < 0 ? '' : p.slice(0, cut)
}

function validate(a) {
  const errors = []
  if (!a || typeof a !== 'object') throw new Error('缺少 args：需要 {base, packages}')
  if (typeof a.base !== 'string' || !/^[0-9a-f]{7,40}$/.test(a.base)) errors.push('base 必须是契约检查点的提交 SHA')
  if (!Array.isArray(a.packages) || a.packages.length === 0) errors.push('packages 不能为空')
  const names = new Set()
  for (const p of a.packages || []) {
    if (!p.name || names.has(p.name)) errors.push(`包名缺失或重复：${p.name}`)
    names.add(p.name)
    if (!p.goal) errors.push(`${p.name}: 缺少 goal`)
    if (!Array.isArray(p.owned) || p.owned.length === 0) errors.push(`${p.name}: owned 不能为空`)
    if (!Array.isArray(p.verify) || p.verify.length === 0) errors.push(`${p.name}: 至少一条 verify 验收命令`)
  }
  // 保守的归属重叠检查：任一 owned 的字面前缀互为前缀即视为重叠
  const pk = a.packages || []
  for (let i = 0; i < pk.length; i++) {
    for (let j = i + 1; j < pk.length; j++) {
      for (const g1 of pk[i].owned || []) {
        for (const g2 of pk[j].owned || []) {
          const p1 = prefixOf(g1), p2 = prefixOf(g2)
          const overlap = p1 === '' || p2 === '' || p1 === p2 || p1.startsWith(p2 + '/') || p2.startsWith(p1 + '/')
          if (overlap) errors.push(`归属重叠：${pk[i].name}(${g1}) 与 ${pk[j].name}(${g2})`)
        }
      }
    }
  }
  if (errors.length) throw new Error('工作包无效：\n- ' + errors.join('\n- '))
}

function casePrompt(p, a) {
  return [
    `为工作包「${p.name}」设计对抗用例，供实现者在写代码前先写成测试。`,
    `目标：${p.goal}`,
    `负责范围：${p.owned.join(', ')}${a.contract ? `\n契约权威来源：${a.contract}` : ''}${p.notes ? `\n关键约束：${p.notes}` : ''}`,
    `基线提交：${a.base}（只读查看现有代码与测试：git show ${a.base}:<path>、git grep）`,
    `只列与本包改动真实相关、会让实现出错的场景，5–12 条，按出错代价与可能性排序。`,
  ].join('\n')
}

function formatCases(cases) {
  return cases.map((c, i) => `${i + 1}. [${c.priority}] ${c.name}：${c.given} → ${c.when} → ${c.then}（防止：${c.prevents}）`).join('\n')
}

function implementPrompt(p, a, cases) {
  const decl = JSON.stringify({ base: a.base, owned: p.owned, forbidden: p.forbidden || [] })
  return [
    `## 目标\n${p.goal}`,
    `## 基线\n- 起点提交：${a.base}（包含共享契约检查点）${p.setup ? `\n- 先执行：${p.setup}` : ''}`,
    `## 归属\n- 你负责（可写）：${p.owned.join(', ')}\n- 禁止修改：${(p.forbidden || []).join(', ') || '归属以外的一切'}\n` +
      `- 开工第一步：用 Write 工具写 worktree 根目录的 .dev-spec-owner.json，内容严格为：\n  ${decl}\n  返回"✓ 归属已记录"即成功；` +
      `若被拒并提示起点不包含基线，说明契约检查点不可见，不要开工，status 返回 blocked 并写明原因。`,
    p.resources ? `- 运行资源：${p.resources}` : '',
    a.contract || p.notes ? `## 契约\n${a.contract ? `- 权威来源：${a.contract}\n` : ''}${p.notes ? `- 关键约束：${p.notes}` : ''}` : '',
    `## 授权\n- 可以：在当前 worktree 分支本地提交\n- 禁止：push、修改归属外文件、用 Bash 写文件绕过 hook`,
    cases && cases.length ? `## 先写成测试的场景（对抗用例）\n先把下列场景写成测试并确认它们在实现前失败（或说明为何不适用），再实现：\n${formatCases(cases)}` : '',
    `## 验收\n${p.verify.map(c => `- ${c}`).join('\n')}\n- 不得删测试、弱化断言或跳过错误来通过`,
    `## 回报\n按 schema 返回：status（done/partial/blocked）、branch、sha（git rev-parse HEAD）、files、` +
      `checks（每条实际运行的命令与真实退出码）、unverified、contract_issues。`,
  ].filter(Boolean).join('\n\n')
}

function reviewPrompt(p, a, rep, cases) {
  return [
    `复核工作包「${p.name}」在分支 ${rep.branch} 上的改动：git diff ${a.base}..${rep.branch}`,
    `目标：${p.goal}`,
    a.contract ? `契约权威来源：${a.contract}` : '',
    p.notes ? `关键约束：${p.notes}` : '',
    `实现者自报的检查：${JSON.stringify(rep.checks)}；未验证面：${JSON.stringify(rep.unverified)}`,
    cases && cases.length ? `对抗用例（逐条核对是否有对应测试；缺失且未说明理由的算 fix-needed）：\n${formatCases(cases)}` : '',
    `重点：是否实现目标、是否与契约及其消费者一致、失败路径与边界、是否删测试或弱化断言、自报检查是否可信。` +
      `只报有触发条件的真实问题；没有问题时 verdict 为 pass、findings 为空数组。`,
  ].filter(Boolean).join('\n')
}

validate(args)
log(`基线 ${args.base.slice(0, 10)}，${args.packages.length} 个工作包`)

// 不依赖 pipeline 传给后续阶段的第二个参数：每个阶段把包本身带下去
const designCases = args.case_design !== false
const results = await pipeline(
  args.packages,
  async p => {
    if (!designCases) return { p, cases: [] }
    try {
      const r = await agent(casePrompt(p, args), { agentType: 'case-designer', schema: CASES, label: `用例 ${p.name}`, phase: '用例设计', effort: 'medium' })
      return { p, cases: r ? r.cases : [], caseNote: r ? undefined : '用例设计代理未返回结果' }
    } catch (e) {
      return { p, cases: [], caseNote: `用例设计代理异常：${e && e.message}` }   // 不阻塞实现，回报中注明
    }
  },
  async ({ p, cases, caseNote }) => {
    const opts = { agentType: 'implementer', isolation: 'worktree', schema: REPORT, label: `实现 ${p.name}`, phase: '实现' }
    if (p.effort) opts.effort = p.effort
    try {
      return { p, cases, caseNote, rep: await agent(implementPrompt(p, args, cases), opts) }
    } catch (e) {
      return { p, cases, caseNote, rep: null, error: `实现代理异常：${e && e.message}` }
    }
  },
  async ({ p, cases, caseNote, rep, error }) => {
    const base = { package: p.name, cases: cases.length, case_note: caseNote }
    if (!rep) return { ...base, report: null, review: null, error: error || '实现代理未返回结果' }
    if (rep.status === 'blocked' || !rep.branch) return { ...base, report: rep, review: null }
    try {
      const review = await agent(reviewPrompt(p, args, rep, cases), {
        agentType: 'reviewer', schema: VERDICT, label: `复核 ${p.name}`, phase: '包级复核',
      })
      return { ...base, report: rep, review, error: review ? undefined : '复核代理未返回结果' }
    } catch (e) {
      return { ...base, report: rep, review: null, error: `复核代理异常：${e && e.message}` }
    }
  },
)

const rows = results.map((r, i) => r || { package: args.packages[i].name, report: null, review: null, error: '阶段异常' })
const ready = rows.filter(r => r.report && r.report.status === 'done' && r.review && r.review.verdict === 'pass')
log(`可集成 ${ready.length}/${rows.length}`)
return {
  base: args.base,
  ready: ready.map(r => r.report.branch),
  needs_attention: rows.filter(r => !ready.includes(r)).map(r => r.package),
  packages: rows,
  next: '在主工作树用 parallel-dev 技能的 scripts/integrate.py（默认 ~/.claude/skills/parallel-dev/scripts/integrate.py）：plan <ready 分支…>，无阻塞后 apply <分支…> --verify "<集成验收命令>"',
}
