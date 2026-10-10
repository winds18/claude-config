// Behaviour tests for workflows/*.js against a stub of the Claude Code workflow runtime.
// Mirrors the documented semantics: agent() returns schema objects or null, pipeline() has no barrier and
// drops a throwing item to null, parallel() is a barrier that resolves throwing thunks to null,
// Date.now()/Math.random() throw, import() is not allowed, meta must be a literal first statement.
// Run: node scripts/test_workflows.mjs
import { readFileSync, readdirSync } from 'node:fs'
import { join, dirname } from 'node:path'
import { fileURLToPath } from 'node:url'

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..')
const results = []
const check = (name, ok, detail = '') => results.push({ name, ok: !!ok, detail })

function load(file) {
  const src = readFileSync(join(ROOT, 'workflows', file), 'utf8')
  return src
}

function staticChecks(file, src) {
  check(`${file}: meta 是首条语句`, /^export const meta = \{/.test(src))
  const metaBlock = src.slice(0, src.indexOf('\n}\n') + 2)
  check(`${file}: meta 只含字面量`, !/(\$\{|\.\.\.|\w\()/.test(metaBlock.replace(/'[^']*'/g, "''")), metaBlock)
  check(`${file}: 不使用 import()`, !/\bimport\s*\(/.test(src))
  check(`${file}: 不使用 Date.now/Math.random/new Date()`, !/Date\.now\(|Math\.random\(|new Date\(\)/.test(src))
  check(`${file}: 不含 TypeScript 语法`, !/\binterface\s+\w|:\s*(string|number|boolean)\[\]/.test(src))
  const nameMatch = src.match(/name:\s*'([^']+)'/)
  check(`${file}: meta.name 与文件名一致`, nameMatch && `${nameMatch[1]}.js` === file)
}

async function run(src, args, agentImpl, { prevOnly = false, nullParallelAt = -1 } = {}) {
  const calls = []
  const runtime = {
    args,
    log: () => {},
    phase: () => {},
    agent: async (prompt, opts = {}) => {
      calls.push({ prompt, opts })
      return agentImpl(prompt, opts, calls.length)
    },
    pipeline: async (items, ...stages) => Promise.all(items.map(async (item, i) => {
      let prev = item
      for (const stage of stages) {
        // prevOnly: a stricter runtime that passes only the previous result, to prove scripts don't depend on (item, index)
        try { prev = prevOnly ? await stage(prev) : await stage(prev, item, i) } catch { return null }
      }
      return prev
    })),
    // nullParallelAt: the runtime nulls one slot (e.g. the user skipped that agent) even though the thunk itself catches
    parallel: async thunks => Promise.all(thunks.map(async (t, i) => { try { const v = await t(); return i === nullParallelAt ? null : v } catch { return null } })),
  }
  const body = src.replace(/^export const meta =/, 'const meta =')
  const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor
  const fn = new AsyncFunction(...Object.keys(runtime), body)
  const realNow = Date.now, realRandom = Math.random
  Date.now = () => { throw new Error('Date.now() is not allowed in workflows') }
  Math.random = () => { throw new Error('Math.random() is not allowed in workflows') }
  try {
    return { value: await fn(...Object.values(runtime)), calls }
  } catch (error) {
    return { error, calls }
  } finally {
    Date.now = realNow; Math.random = realRandom
  }
}

// ---------- dev-spec-implement ----------
async function testImplement() {
  const src = load('dev-spec-implement.js')
  staticChecks('dev-spec-implement.js', src)
  const base = 'a1b2c3d4e5'
  const pkgs = [
    { name: 'api', goal: '实现订单 API', owned: ['src/api/**'], forbidden: ['src/types/**'], verify: ['pytest tests/api'], effort: 'medium' },
    { name: 'web', goal: '订单页面', owned: ['src/web/**'], verify: ['npm test -- web'] },
    { name: 'blocked', goal: '依赖未就绪', owned: ['src/jobs/**'], verify: ['pytest tests/jobs'] },
  ]
  const impl = (prompt, opts) => {
    if (opts.agentType === 'implementer') {
      const name = opts.label.replace('实现 ', '')
      if (name === 'blocked') return { status: 'blocked', branch: '', sha: '', files: [], checks: [], unverified: [], contract_issues: ['契约缺字段'] }
      return { status: 'done', branch: `worktree-${name}`, sha: 'f'.repeat(40), files: [`src/${name}/x.py`],
        checks: [{ command: 'pytest', exit_code: 0, summary: 'ok' }], unverified: [], contract_issues: [] }
    }
    if (opts.agentType === 'reviewer') {
      return opts.label.includes('web') ? { verdict: 'fix-needed', findings: [{ severity: 'high', location: 'src/web/x.py:3', problem: 'p', trigger: 't' }] }
        : { verdict: 'pass', findings: [] }
    }
  }
  const r = await run(src, { base, contract: 'src/types/order.ts#Order', packages: pkgs }, impl)
  check('implement: 正常运行', !r.error, r.error && r.error.message)
  const implCalls = r.calls.filter(c => c.opts.agentType === 'implementer')
  check('implement: 每包一个 implementer', implCalls.length === 3)
  check('implement: 全部 worktree 隔离 + 结构化 schema', implCalls.every(c => c.opts.isolation === 'worktree' && c.opts.schema))
  check('implement: effort 按包透传', implCalls[0].opts.effort === 'medium' && !('effort' in implCalls[1].opts))
  const api = implCalls[0].prompt
  check('implement: 提示含归属声明 JSON', api.includes(JSON.stringify({ base, owned: ['src/api/**'], forbidden: ['src/types/**'] })))
  check('implement: 提示含验收与禁止项', api.includes('pytest tests/api') && api.includes('禁止：push'))
  const reviews = r.calls.filter(c => c.opts.agentType === 'reviewer')
  check('implement: blocked 包不复核', reviews.length === 2 && !reviews.some(c => c.opts.label.includes('blocked')))
  check('implement: 复核提示指向真实 diff', reviews[0].prompt.includes(`git diff ${base}..worktree-`))
  check('implement: 只有 done+pass 才可集成', JSON.stringify(r.value.ready) === JSON.stringify(['worktree-api']))
  check('implement: 其余列入需处理', JSON.stringify(r.value.needs_attention) === JSON.stringify(['web', 'blocked']))

  const nullAgent = await run(src, { base, packages: pkgs.slice(0, 1) }, (p, o) => (o.agentType === 'implementer' ? null : { verdict: 'pass', findings: [] }))
  check('implement: 代理返回 null 时不崩溃并标注', !nullAgent.error && nullAgent.value.packages[0].error)

  const bad = [
    [{ packages: pkgs }, 'base'],
    [{ base, packages: [] }, 'packages'],
    [{ base, packages: [{ name: 'a', goal: 'g', owned: ['src/**'], verify: ['x'] }, { name: 'b', goal: 'g', owned: ['src/api/**'], verify: ['x'] }] }, '归属重叠'],
    [{ base, packages: [{ name: 'a', goal: 'g', owned: ['src/a/**'], verify: [] }] }, 'verify'],
    [{ base, packages: [{ name: 'a', goal: 'g', owned: ['src/a/**'], verify: ['x'] }, { name: 'a', goal: 'g', owned: ['src/b/**'], verify: ['x'] }] }, '重复'],
  ]
  for (const [args, needle] of bad) {
    const res = await run(src, args, () => { throw new Error('should not spawn') })
    check(`implement: 拒绝无效参数（${needle}）且不派发`, res.error && res.error.message.includes(needle) && res.calls.length === 0, res.error && res.error.message)
  }
  const ok = await run(src, { base, packages: [{ name: 'a', goal: 'g', owned: ['src/a/**'], verify: ['x'] }, { name: 'b', goal: 'g', owned: ['src/ab/**'], verify: ['x'] }] }, impl)
  check('implement: 相邻但不重叠的目录不误报', !ok.error, ok.error && ok.error.message)

  // 回归：复核报告指出的重叠漏报（./ 前缀、通配符在目录名中间）
  const pair = (g1, g2) => ({ base, packages: [{ name: 'a', goal: 'g', owned: [g1], verify: ['x'] }, { name: 'b', goal: 'g', owned: [g2], verify: ['x'] }] })
  for (const [g1, g2] of [['./src/api/**', 'src/api/**'], ['./src/**', 'src/api/**'], ['src/a?i/**', 'src/abi/**'], ['src/api*', 'src/apix/**']]) {
    const res = await run(src, pair(g1, g2), () => { throw new Error('should not spawn') })
    check(`implement: 重叠检查覆盖 ${g1} vs ${g2}`, res.error && res.error.message.includes('归属重叠') && res.calls.length === 0, res.error && res.error.message)
  }

  // 用例设计阶段：用例注入实现与复核提示；失败不阻塞；可关闭
  const CASE = { priority: 'high', name: '改名绕过', given: 'forbidden 下有文件', when: 'git mv 到 owned', then: '被判越界', prevents: '漏检越界' }
  const withCases = (p, o) => (o.agentType === 'case-designer' ? { cases: [CASE] } : impl(p, o))
  const wc = await run(src, { base, packages: pkgs.slice(0, 1) }, withCases)
  const designer = wc.calls.filter(c => c.opts.agentType === 'case-designer')
  check('implement: 每包先跑一次用例设计（只读、medium）', designer.length === 1 && designer[0].opts.effort === 'medium' && !designer[0].opts.isolation)
  const implPrompt = wc.calls.find(c => c.opts.agentType === 'implementer').prompt
  check('implement: 用例注入实现提示并要求先写测试', implPrompt.includes('先写成测试的场景') && implPrompt.includes('改名绕过'))
  const revPrompt = wc.calls.find(c => c.opts.agentType === 'reviewer').prompt
  check('implement: 复核逐条核对用例', revPrompt.includes('逐条核对') && revPrompt.includes('改名绕过'))
  check('implement: 回报带用例数量', wc.value.packages[0].cases === 1)
  const order = wc.calls.map(c => c.opts.agentType).join('>')
  check('implement: 顺序为 用例→实现→复核', order === 'case-designer>implementer>reviewer', order)
  const caseCrash = await run(src, { base, packages: pkgs.slice(0, 1) }, (p, o) => { if (o.agentType === 'case-designer') throw new Error('x'); return impl(p, o) })
  check('implement: 用例设计失败不阻塞实现并注明', caseCrash.value && caseCrash.value.ready.length === 1 && /用例设计代理异常/.test(caseCrash.value.packages[0].case_note),
    JSON.stringify(caseCrash.value && caseCrash.value.packages[0]))
  const lowPkg = [{ ...pkgs[1], effort: 'low' }, { ...pkgs[0], effort: undefined }]
  const mixed = await run(src, { base, packages: lowPkg }, withCases)
  const designed = mixed.calls.filter(c => c.opts.agentType === 'case-designer').map(c => c.opts.label)
  check('implement: effort low 的包跳过用例设计，其余照常', designed.length === 1 && designed[0] === '用例 api', designed.join(','))
  check('implement: 包级复核提示含相称性约束', mixed.calls.find(c => c.opts.agentType === 'reviewer').prompt.includes('不构成 fix-needed'))
  const modelRun = await run(src, { base, packages: [{ ...pkgs[0], model: 'sonnet' }, pkgs[1]] }, withCases)
  const implModels = modelRun.calls.filter(c => c.opts.agentType === 'implementer').map(c => c.opts.model)
  check('implement: model 按包透传，未指定的不带该字段', implModels[0] === 'sonnet' && implModels[1] === undefined, JSON.stringify(implModels))
  const noCase = await run(src, { base, packages: pkgs.slice(0, 1), case_design: false }, withCases)
  check('implement: case_design=false 跳过用例阶段', !noCase.calls.some(c => c.opts.agentType === 'case-designer'))

  // 回归：不依赖 pipeline 传给后续阶段的第二个参数
  const strict = await run(src, { base, contract: 'c', packages: pkgs }, impl, { prevOnly: true })
  check('implement: 运行时只传 prev 时结果不变', !strict.error && JSON.stringify(strict.value.ready) === JSON.stringify(['worktree-api'])
    && strict.value.packages.every(r => r.package), strict.error ? strict.error.message : JSON.stringify(strict.value && strict.value.packages))

  // 回归：复核代理抛错时保留实现者回报
  const reviewCrash = await run(src, { base, packages: pkgs.slice(0, 1) }, (p, o) => {
    if (o.agentType === 'reviewer') throw new Error('boom')
    return impl(p, o)
  })
  const row = reviewCrash.value && reviewCrash.value.packages[0]
  check('implement: 复核异常时保留分支与检查结果', row && row.report && row.report.branch === 'worktree-api' && /复核代理异常/.test(row.error)
    && reviewCrash.value.ready.length === 0, JSON.stringify(row))
  const implCrash = await run(src, { base, packages: pkgs.slice(0, 1) }, () => { throw new Error('dead') })
  check('implement: 实现代理异常被记录而非静默', implCrash.value && /实现代理异常/.test(implCrash.value.packages[0].error), JSON.stringify(implCrash.value))
}

// ---------- dev-spec-review ----------
async function testReview() {
  const src = load('dev-spec-review.js')
  staticChecks('dev-spec-review.js', src)
  const finder = (prompt, opts) => {
    if (opts.phase === '发现') {
      if (opts.label.endsWith('correctness')) return { findings: [
        { severity: 'medium', file: 'a.py', line: 10, problem: '空列表时除零', trigger: 'items=[]' },
        { severity: 'low', file: 'b.py', line: 5, problem: '不存在的问题', trigger: 'x' },
      ] }
      if (opts.label.endsWith('tests')) return { findings: [{ severity: 'high', file: 'a.py', line: 11, problem: '空列表时除零', trigger: 'items=[]' }] }
      if (opts.label.endsWith('security')) throw new Error('agent crashed')
      return { findings: [] }
    }
    if (opts.label.includes('b.py')) return { verdict: 'refuted', evidence: '有前置校验' }
    if (opts.label.includes('a.py')) return { verdict: 'confirmed', evidence: 'tests/test_a.py 复现' }
    return { verdict: 'uncertain', evidence: '' }
  }
  const r = await run(src, { range: 'abc123' }, finder)
  check('review: 正常运行', !r.error, r.error && r.error.message)
  const finders = r.calls.filter(c => c.opts.phase === '发现')
  check('review: 默认 5 个视角并行', finders.length === 5)
  check('review: 安全视角用 security-reviewer', finders.some(c => c.opts.agentType === 'security-reviewer'))
  check('review: 单一视角范围归一为 ..HEAD', finders[0].prompt.includes('abc123..HEAD'))
  const verifies = r.calls.filter(c => c.opts.phase === '对抗验证')
  check('review: 跨视角重复发现被合并', verifies.length === 2, verifies.map(v => v.opts.label).join(','))
  check('review: 合并时保留最高严重度与视角', r.value.confirmed.length === 1 && r.value.confirmed[0].severity === 'high'
    && r.value.confirmed[0].lenses.length === 2, JSON.stringify(r.value.confirmed))
  check('review: 被证伪的问题不输出', r.value.refuted === 1 && !r.value.confirmed.some(f => f.file === 'b.py'))
  check('review: 单个视角崩溃不影响整体', !r.error)

  check('review: 发现阶段默认 effort medium', finders.every(c => c.opts.effort === 'medium'))
  check('review: 验证阶段默认继承会话 effort', verifies.every(c => !('effort' in c.opts)))
  const eff = await run(src, { range: 'a..b', lenses: ['correctness'], finder_effort: 'high', verify_effort: 'xhigh' },
    (p, o) => (o.phase === '发现' ? { findings: [{ severity: 'low', file: 'q.py', line: 1, problem: 'p', trigger: 't' }] } : { verdict: 'confirmed', evidence: 'e' }))
  check('review: effort 可由 args 覆盖', eff.calls[0].opts.effort === 'high' && eff.calls[1].opts.effort === 'xhigh')

  const empty = await run(src, { range: 'a..b' }, () => ({ findings: [] }))
  check('review: 无候选时提前结束、不做验证', empty.value.confirmed.length === 0 && empty.calls.length === 5)
  const some = await run(src, { range: 'a..b', lenses: ['security', 'nope'] }, () => ({ findings: [] }))
  check('review: 只跑指定的有效视角', some.calls.length === 1)
  const bad = await run(src, {}, () => ({ findings: [] }))
  check('review: 缺少 range 时报错', bad.error && bad.calls.length === 0)

  // 回归：崩溃的视角不能被计入覆盖面
  check('review: 崩溃视角列入 failed_lenses 且不计入 lenses', r.value.failed_lenses.includes('security') && !r.value.lenses.includes('security'),
    JSON.stringify({ lenses: r.value.lenses, failed: r.value.failed_lenses }))
  // 回归：验证代理崩溃时发现保留为 uncertain，不静默丢弃
  const vcrash = await run(src, { range: 'a..b', lenses: ['correctness'] }, (p, o) => {
    if (o.phase === '发现') return { findings: [{ severity: 'critical', file: 'x.py', line: 1, problem: '越权', trigger: 't' }] }
    throw new Error('verifier died')
  })
  check('review: 验证异常的发现归入 uncertain', vcrash.value && vcrash.value.uncertain.length === 1 && vcrash.value.uncertain[0].severity === 'critical',
    JSON.stringify(vcrash.value))
  const skipped = await run(src, { range: 'abc123', lenses: ['correctness', 'security', 'tests'] }, finder, { nullParallelAt: 0 })
  check('review: failed_lenses 按原始位置标注（回归）', skipped.value && JSON.stringify(skipped.value.failed_lenses) === JSON.stringify(['correctness', 'security'])
    && JSON.stringify(skipped.value.lenses) === JSON.stringify(['tests']), JSON.stringify(skipped.value && { f: skipped.value.failed_lenses, l: skipped.value.lenses }))
  const strictR = await run(src, { range: 'abc123' }, finder, { prevOnly: true })
  check('review: 运行时只传 prev 时结果不变', !strictR.error && strictR.value.confirmed.length === 1, strictR.error && strictR.error.message)
}

// --validate-args <file>: run the real dev-spec-implement validation on a JSON args file (used by test_dispatch.py
// to check producer/consumer agreement across languages). Prints {"ok": bool, "error": string|null}.
if (process.argv[2] === '--validate-args') {
  const args = JSON.parse(readFileSync(process.argv[3], 'utf8'))
  const ok = { status: 'done', branch: 'b', sha: 's', files: [], checks: [], unverified: [], contract_issues: [] }
  const r = await run(load('dev-spec-implement.js'), args, (p, o) => (o.agentType === 'reviewer' ? { verdict: 'pass', findings: [] }
    : o.agentType === 'case-designer' ? { cases: [] } : ok))
  console.log(JSON.stringify({ ok: !r.error, error: r.error ? r.error.message : null, case_design_calls: r.calls.filter(c => c.opts.agentType === 'case-designer').length }))
  process.exit(0)
}

await testImplement()
await testReview()
const names = readdirSync(join(ROOT, 'workflows')).filter(f => f.endsWith('.js'))
check('所有 workflow 都有测试', names.every(n => ['dev-spec-implement.js', 'dev-spec-review.js'].includes(n)), names.join(','))

const failed = results.filter(r => !r.ok)
for (const f of failed) console.log(`FAIL ${f.name}: ${f.detail}`)
console.log(`workflows: ${results.length - failed.length}/${results.length} passed`)
process.exit(failed.length ? 1 : 0)
