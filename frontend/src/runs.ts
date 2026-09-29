// Labels shared by the run dock and the Agents view.

import type { AgentRun } from './api'

export const WORKFLOW_LABEL = { canon_check: 'Canon check', expand_frontier: 'Expand' } as const

export function describeProgress(run: AgentRun, passageCount = 0): string {
  const p = run.progress
  if (run.workflow === 'canon_check') return `${p.checked ?? 0}/${run.scope.length || passageCount || 'all'} checked`
  const b = run.budget
  const cap = b.max_passages != null ? `/${b.max_passages}` : ''
  let s = `${p.created ?? 0}${cap} passages · level ${(p.level ?? 0) + 1}/${b.depth ?? '?'} · ≤${b.branching ?? '?'} choices`
  if (p.retries) s += ` · ${p.retries} retr${p.retries === 1 ? 'y' : 'ies'}`
  if (p.rounds) s += ` · ${p.rounds} audit round${p.rounds === 1 ? '' : 's'}`
  if (p.restarts) s += ` · ${p.restarts} restart${p.restarts === 1 ? '' : 's'}`
  return s
}
