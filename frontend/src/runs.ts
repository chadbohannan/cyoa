// Labels shared by the run dock and the Agents view.

import type { AgentRun } from './api'

export const WORKFLOW_LABEL = { canon_check: 'Canon check', expand_frontier: 'Expand' } as const

export function describeProgress(run: AgentRun, passageCount = 0): string {
  const p = run.progress
  if (run.workflow === 'canon_check') return `${p.checked ?? 0}/${run.scope.length || passageCount || 'all'} checked`
  const b = run.budget
  return `${p.created ?? 0}/${b.max_passages ?? '?'} passages · level ${(p.level ?? 0) + 1}/${b.depth ?? '?'}`
}
