export const meta = {
  name: 'dev-spec-review',
  description: 'dev-spec 多视角复核：按视角并行找问题，去重后逐条对抗验证，只输出被证实的问题',
  phases: ['发现', '对抗验证'],
}

// args: { range: "<base>..<head>" | "<base>", focus?: "<额外关注点>", lenses?: ["correctness", ...] }
// 视角可选：correctness, contract, security, performance, tests

const LENSES = {
  correctness: { agentType: 'reviewer', ask: '正确性与回归：边界条件、空值/异常路径、并发与状态、错误处理是否吞错' },
  contract: { agentType: 'reviewer', ask: '契约一致性：接口/类型/schema 变化后，所有生产者与消费者、文档、测试是否同步；错误语义是否一致' },
  security: { agentType: 'security-reviewer', ask: '安全：鉴权与越权、注入、敏感数据进入日志/URL/提交、密钥硬编码、危险默认值' },
  performance: { agentType: 'reviewer', ask: '性能与复杂度：N+1、重复扫描或 I/O、无界内存/并发、复杂度退化，需按实际规模说明影响' },
  tests: { agentType: 'reviewer', ask: '验证充分性：改动行为是否有测试覆盖失败路径；是否删测试、弱化断言、跳过错误或用 mock 冒充集成' },
}

const FINDINGS = {
  type: 'object',
  required: ['findings'],
  properties: {
    findings: {
      type: 'array',
      items: {
        type: 'object',
        required: ['severity', 'file', 'line', 'problem', 'trigger'],
        properties: {
          severity: { type: 'string', enum: ['critical', 'high', 'medium', 'low'] },
          file: { type: 'string' },
          line: { type: 'integer' },
          problem: { type: 'string' },
          trigger: { type: 'string' },
        },
      },
    },
  },
}

const VERIFY = {
  type: 'object',
  required: ['verdict', 'evidence'],
  properties: {
    verdict: { type: 'string', enum: ['confirmed', 'refuted', 'uncertain'] },
    evidence: { type: 'string' },
    severity: { type: 'string', enum: ['critical', 'high', 'medium', 'low'] },
  },
}

const RANK = { critical: 0, high: 1, medium: 2, low: 3 }

// 同一文件、行号相差 ≤2 且问题描述开头相同才视为重复；宁可多验证一条，也不把不同问题合并掉。
function dedupe(items) {
  const out = []
  const head = s => String(s).replace(/\s+/g, '').slice(0, 6)
  for (const f of items) {
    const dup = out.find(g => g.file === f.file && Math.abs((g.line || 0) - (f.line || 0)) <= 2 && head(g.problem) === head(f.problem))
    if (!dup) { out.push({ ...f, lenses: [f.lens] }); continue }
    dup.lenses = [...new Set([...dup.lenses, f.lens])]
    if (RANK[f.severity] < RANK[dup.severity]) Object.assign(dup, { severity: f.severity, line: f.line, trigger: f.trigger })
  }
  return out
}

if (!args || typeof args.range !== 'string' || !args.range.trim()) throw new Error('缺少 args.range，例如 "abc123..HEAD"')
const range = args.range.includes('..') ? args.range : `${args.range}..HEAD`
const lenses = (args.lenses && args.lenses.length ? args.lenses : Object.keys(LENSES)).filter(l => LENSES[l])
if (!lenses.length) throw new Error(`没有有效视角，可选：${Object.keys(LENSES).join(', ')}`)

phase('发现')
const found = await parallel(lenses.map(lens => async () => {
  try {
    const r = await agent(
      `只读复核 git diff ${range}（先 git diff --stat ${range} 了解范围，再读受影响的调用方与被调用方）。\n` +
      `本轮只看一个视角——${LENSES[lens].ask}。\n${args.focus ? `额外关注：${args.focus}\n` : ''}` +
      `每条发现必须给出可复现的触发条件与错误结果；风格偏好、泛泛的"建议加测试"不算。没有发现就返回空数组。`,
      { agentType: LENSES[lens].agentType, schema: FINDINGS, label: `发现·${lens}`, phase: '发现' },
    )
    return r ? { lens, findings: r.findings.map(f => ({ ...f, lens })) } : { lens, failed: true, findings: [] }
  } catch (e) {
    return { lens, failed: true, findings: [] }
  }
}))

const failedLenses = found.map((r, i) => (!r || r.failed ? lenses[i] : null)).filter(Boolean)  // index on the unfiltered array
const covered = lenses.filter(l => !failedLenses.includes(l))
const candidates = dedupe(found.filter(Boolean).flatMap(r => r.findings))
log(`候选问题 ${candidates.length} 条（完成 ${covered.length}/${lenses.length} 个视角）`)
if (candidates.length === 0) return { range, lenses: covered, failed_lenses: failedLenses, confirmed: [], uncertain: [], refuted: 0 }

const verdicts = await pipeline(candidates, async f => {
  try {
    const v = await agent(
      `你是对抗验证者：尽力证伪下面这条复核发现，只有在无法证伪时才判 confirmed。\n` +
      `范围：git diff ${range}\n发现：${f.file}:${f.line} [${f.severity}] ${f.problem}\n声称的触发条件：${f.trigger}\n` +
      `方法：阅读真实代码路径；能用只读命令或现有测试验证的就去验证。证据不足时判 uncertain，不要猜。`,
      { agentType: 'reviewer', schema: VERIFY, label: `验证 ${f.file}:${f.line}`, phase: '对抗验证' },
    )
    return { ...f, verdict: v ? v.verdict : 'uncertain', evidence: v ? v.evidence : '验证代理未返回结果', severity: (v && v.severity) || f.severity }
  } catch (e) {
    return { ...f, verdict: 'uncertain', evidence: `验证代理异常：${e && e.message}` }
  }
})

// 防御：任何意外丢失的条目都按 uncertain 保留，不静默消失
const all = verdicts.map((v, i) => v || { ...candidates[i], verdict: 'uncertain', evidence: '验证阶段异常' })
const bySeverity = (x, y) => RANK[x.severity] - RANK[y.severity]
return {
  range,
  lenses: covered,
  failed_lenses: failedLenses,
  confirmed: all.filter(v => v.verdict === 'confirmed').sort(bySeverity),
  uncertain: all.filter(v => v.verdict === 'uncertain').sort(bySeverity),
  refuted: all.filter(v => v.verdict === 'refuted').length,
}
