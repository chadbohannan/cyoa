// Labels shared by the run dock and the Agents view.

import type { AgentRun, PacingPlan, RunBudget } from './api'
import { planDepth, stageStarts } from './usePacing'

export const WORKFLOW_LABEL = { canon_check: 'Canon check', expand_frontier: 'Expand' } as const

// The name of the stage a level belongs to: an act's, or the ending's.
export function stageName(plan: Pick<PacingPlan, 'acts'>, level: number): string {
  const starts = stageStarts(plan)
  const i = plan.acts.findIndex((_, j) => level < starts[j + 1])
  return i < 0 ? 'Resolution' : plan.acts[i].name
}

// "Setup 1 · Confrontation 2 · Climax 1 → Resolution · depth 5"
export function describePlan(plan: Pick<PacingPlan, 'acts'>): string {
  return `${plan.acts.map((a) => `${a.name} ${a.levels}`).join(' · ')} → Resolution · depth ${planDepth(plan)}`
}

export function describeProgress(run: AgentRun, passageCount = 0): string {
  const p = run.progress
  if (run.workflow === 'canon_check') return `${p.checked ?? 0}/${run.scope.length || passageCount || 'all'} checked`
  const b: RunBudget = run.budget
  const level = p.level ?? 0
  let s = `${p.created ?? 0} passages · level ${level + 1}/${b.depth ?? '?'}`
  if (b.acts) s += ` · ${stageName({ acts: b.acts }, level)}`
  if (p.retries) s += ` · ${p.retries} retr${p.retries === 1 ? 'y' : 'ies'}`
  if (p.rounds) s += ` · ${p.rounds} audit round${p.rounds === 1 ? '' : 's'}`
  if (p.restarts) s += ` · ${p.restarts} restart${p.restarts === 1 ? '' : 's'}`
  return s
}
