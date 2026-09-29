// Typed gateway to the FastAPI backend (proxied at /api by Vite in dev).
// Mirrors backend/src/cyoa/models.py field for field.

export interface Story {
  id: number
  title: string
  description: string
  ifid: string
  start_passage_id: number | null
  created_at: string
  updated_at: string
}

export interface StorySummary extends Story {
  passage_count: number
}

export type Provenance = 'human' | 'agent'

export interface AuditIssue {
  severity: 'minor' | 'major'
  problem: string
  quote: string
  evidence: string
}

export interface AuditMeta {
  verdict: 'unresolved'
  rounds: number
  issues: AuditIssue[]
}

export interface PassageMeta {
  location?: string
  present?: string[]
  events?: number[]
  audit?: AuditMeta
  [key: string]: unknown
}

export interface Passage {
  id: number
  story_id: number
  title: string
  body: string
  pos_x: number
  pos_y: number
  t: number | null
  meta: PassageMeta
  reviewed: boolean
  provenance: Provenance
  is_ending: boolean // a decided, terminal passage; authoritative over any outgoing choices at play time
}

export interface Choice {
  id: number
  story_id: number
  from_passage: number
  to_passage: number | null
  text: string
  sort_order: number
  condition: Record<string, unknown>
  effects: Record<string, unknown>
}

export type VariableKind = 'flag' | 'stat' | 'item'

export interface Variable {
  id: number
  story_id: number
  name: string
  kind: VariableKind
  default: unknown
}

export type LoreKind = 'character' | 'place' | 'faction' | 'rule'
export const LORE_KINDS: LoreKind[] = ['character', 'place', 'faction', 'rule']

export interface Lore {
  id: number
  story_id: number
  kind: LoreKind
  name: string
  body: string
  reviewed: boolean
}

export interface StoryEvent {
  id: number
  story_id: number
  t: number
  name: string
  body: string
  reviewed: boolean
}

// --- proposals (agent output; only Accept writes to the tables above) ---------

export type ProposalKind = 'new_passage' | 'new_choice' | 'edit_passage' | 'edit_choice' | 'new_variable' | 'note'
export type AssistVerb = 'draft_body' | 'suggest_choices' | 'draft_target' | 'rewrite' | 'suggest_next'

// payload by kind mirrors the docstring on models.Proposal
export interface NewTarget { title: string; body?: string; pos_x?: number; pos_y?: number }
export interface Proposal {
  id: number
  story_id: number
  batch: string
  verb: string
  kind: ProposalKind
  target_passage: number | null
  target_choice: number | null
  payload: {
    body?: string
    is_ending?: boolean
    reasoning?: string // suggest_next's rationale, on an is_ending edit_passage proposal
    title?: string
    text?: string
    to_passage?: number | null
    new_target?: NewTarget | null
    pos_x?: number
    pos_y?: number
    name?: string
    kind?: VariableKind
    default?: unknown
    issues?: { claim: string; conflicts_with: string; severity: 'minor' | 'major' }[]
    audit?: AuditMeta // new_choice, when refine gave up on the target but kept it (see agents.DEFAULT_REFINE)
  }
  status: 'pending' | 'accepted' | 'rejected'
  run_id: number | null
  created_at: string
}

export interface AssistRequest {
  verb: AssistVerb
  model?: string | null
  tone?: string | null
  choice_id?: number | null
}

// --- runs (autonomous workflows; see agents.WORKFLOWS) -----------------------

export type Workflow = 'canon_check' | 'expand_frontier'
export type RunStatus = 'running' | 'paused' | 'done' | 'failed' | 'cancelled'

// expand_frontier's draft/audit/revise loop (agents.DEFAULT_REFINE); rounds: 0 is the
// pre-R1 no-op path.
export interface RefinePolicy {
  rounds?: number
  restarts?: number
  block?: 'minor' | 'major'
  on_unresolved?: 'keep_flagged' | 'drop'
  candidates?: number
  votes?: number
  audit_reasoning?: boolean
  path_audit?: boolean
  auditors?: { passage?: string[]; choices?: string[] }
  audit_model?: string | null
}

export interface RunPolicy {
  auto_accept_unreviewed?: boolean
  skip_review_interrupt?: boolean
  refine?: RefinePolicy
}

export interface RunProgress {
  level?: number
  created?: number
  frontier?: number
  checked?: number
  retries?: number
  rounds?: number
  restarts?: number
}

// A story's pacing plan (models.PacingPlan; concepts/act-pacing.md). Depth is derived:
// the acts' levels plus one for the ending.
export interface PacingAct {
  name: string
  levels: number
  branching: number // most choices out of a passage in this act
  ends_story: boolean // a storyline may end early inside this act
  length: string // target prose length, as the prompt says it
  guidance: string
}

export interface PacingPlan {
  acts: PacingAct[]
  ending: { length: string; guidance: string }
  steer: string
  max_seconds: number
  policy: Pick<RunPolicy, 'auto_accept_unreviewed' | 'refine'>
}

// What a run keeps of its plan: a copy, plus the derived depth.
export type RunBudget = Partial<PacingPlan> & { depth?: number }

export interface RunCreate {
  workflow: Workflow
  scope: number[]
  model?: string | null
  steer?: string | null // over the plan's
  budget?: PacingPlan | Record<string, never> // empty: the story's saved plan
  policy?: RunPolicy // over the plan's
}

export interface AgentRun {
  id: number
  story_id: number
  workflow: Workflow
  thread_id: string
  // `prompts` is the run's snapshot of every role, taken at start (runs.py)
  params: { model?: string | null; steer?: string | null; prompts?: Record<string, string> }
  scope: number[]
  budget: RunBudget
  policy: RunPolicy
  status: RunStatus
  progress: RunProgress
  error: string | null
  pending_proposals: number
  created_at: string
  updated_at: string
}

// What a run streams over SSE (runs.py `_push`); `seq` orders them.
export type RunEvent = { seq: number; at: number } & (
  | { type: 'node'; node: string }
  | { type: 'activity'; passage_id: number; phase: 'check' | 'choices' | 'passage' | 'done'; choice?: string }
  // the run's whole progress dict, as the backend stores it (see AgentRun.progress)
  | ({ type: 'progress' } & RunProgress)
  | { type: 'interrupt'; value: { level: number; created: number; next: number } }
  | { type: 'status'; status: RunStatus; error: string | null }
)

export interface RunState {
  values: Record<string, unknown>
  next: string[]
  history: { step: number | null; next: string[]; created_at: string | null }[]
}

// One step of the loop the Agents view draws (agents.FRONTIER_LOOP, or a graph's own nodes).
export interface LoopStep {
  id: string
  label: string
  roles: string[] // prompt roles it composes
  reads: string[] // plan fields it takes; act fields unprefixed
  when?: string // the condition it runs under
  inside?: string // the graph node it runs inside
  doc?: string
}

// compiled.get_graph().to_json(); `prompts` is the roles a node composes (agents.NODE_PROMPTS)
export interface WorkflowGraph {
  nodes: { id: string; type?: string; data?: unknown; prompts: string[] }[]
  edges: { source: string; target: string; conditional?: boolean }[]
  loop: { steps: LoopStep[]; edges: [string, string][] }
}

// --- prompts (global roles; see prompts.py) ------------------------------------

export interface PromptInfo {
  key: string
  label: string
  doc: string
  placeholders: string[]
  used_by: [workflow: string, node: string][]
  default: string
  override: string | null // null while the default is in use
}

export interface PromptUpdate {
  text: string
}

export interface PromptPreviewRequest {
  passage_id: number
  text?: string | null // an unsaved draft to preview instead of the saved text
  stage?: number | null // fill the pacing slots as this stage of the plan would (len(acts) = the ending)
  plan?: PacingPlan | null // an unsaved plan to preview instead of the story's saved one
}

export interface StoryGraph {
  story: Story
  passages: Passage[]
  choices: Choice[]
  variables: Variable[]
  lore: Lore[]
  events: StoryEvent[]
  proposals: Proposal[] // pending only
}

// --- play & validation -------------------------------------------------------

export type PlayState = Record<string, unknown>

export interface PlayView {
  passage: Passage
  choices: Choice[]
  state: PlayState
}

export interface Problems {
  no_start: boolean
  unreachable: number[]
  dangling: number[]
  undeclared: { choice_id: number; var: string }[]
  endings: number[] // is_ending passages: decided, terminal
  unfinished: number[] // no outgoing choices, but not decided as an ending
  merge_candidates: MergeCandidate[]
}

// Passages sharing story time, location and events: one scene reached by several routes.
export interface MergeCandidate {
  t: number
  location: string
  events: string[]
  passage_ids: number[]
}

// What pruning a passage deleted: its subtree (not the passage itself) and every choice out of either.
export interface PruneResult {
  passage_ids: number[]
  choice_ids: number[]
}

export interface PromoteRequest {
  root?: number | null // its subtree (passages reachable only through it), as the map's collapse defines it
  passage_ids?: number[]
  reviewed?: boolean
}

// Coverage from random playthroughs; the counters are keyed by String(id) as JSON has it.
export interface Playtest {
  runs: number
  mean_length: number
  cut: number
  visited: Record<string, number>
  unvisited: number[]
  untaken: number[]
  endings: Record<string, number> // landed on an is_ending passage
  unfinished: Record<string, number> // landed on a stub: no choices, not decided as an ending
  stuck: Record<string, number>
  dangling: Record<string, number>
}

type Omitted = 'id' | 'story_id'
export type PassageInput = Partial<Omit<Passage, Omitted>> & { title: string }
export type ChoiceInput = Partial<Omit<Choice, Omitted>> & { from_passage: number; text: string }
export type VariableInput = Omit<Variable, Omitted>
export type LoreInput = Partial<Omit<Lore, Omitted>> & { kind: LoreKind; name: string }
export type EventInput = Partial<Omit<StoryEvent, Omitted>> & { t: number; name: string }

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`/api${path}`, {
    headers: { 'Content-Type': 'application/json' },
    ...init,
  })
  if (!res.ok) throw new Error(`${res.status} ${await res.text()}`)
  return res.status === 204 ? (undefined as T) : res.json()
}

const post = <T>(path: string, body?: unknown) =>
  request<T>(path, { method: 'POST', body: body === undefined ? undefined : JSON.stringify(body) })
const patch = <T>(path: string, body: unknown) =>
  request<T>(path, { method: 'PATCH', body: JSON.stringify(body) })
const del = (path: string) => request<void>(path, { method: 'DELETE' })

// One CRUD triple per child table; the URL shape is identical for all of them.
function child<Row, Input>(table: string) {
  return {
    create: (storyId: number, body: Input) => post<Row>(`/stories/${storyId}/${table}`, body),
    update: (storyId: number, id: number, body: Partial<Input>) =>
      patch<Row>(`/stories/${storyId}/${table}/${id}`, body),
    remove: (storyId: number, id: number) => del(`/stories/${storyId}/${table}/${id}`),
  }
}

export const api = {
  health: () => request<{ ok: boolean }>('/health'),
  stories: () => request<StorySummary[]>('/stories'),
  createStory: (title: string, description = '') =>
    post<StorySummary>('/stories', { title, description }),
  updateStory: (storyId: number, body: Partial<Pick<Story, 'title' | 'description' | 'start_passage_id'>>) =>
    patch<Story>(`/stories/${storyId}`, body),
  deleteStory: (storyId: number) => del(`/stories/${storyId}`),
  graph: (storyId: number) => request<StoryGraph>(`/stories/${storyId}/graph`),

  passages: {
    ...child<Passage, PassageInput>('passages'),
    duplicate: (storyId: number, id: number) => post<Passage>(`/stories/${storyId}/passages/${id}/duplicate`),
    // delete the passage's choices and its subtree, leaving it a frontier
    prune: (storyId: number, id: number) => post<PruneResult>(`/stories/${storyId}/passages/${id}/prune`),
    // fold `passageIds` into `into`: inbound choices retargeted, outgoing moved, the others deleted
    merge: (storyId: number, into: number, passageIds: number[]) =>
      post<Passage>(`/stories/${storyId}/passages/merge`, { into, passage_ids: passageIds }),
  },
  promote: (storyId: number, body: PromoteRequest) => post<{ passage_ids: number[] }>(`/stories/${storyId}/promote`, body),
  choices: child<Choice, ChoiceInput>('choices'),
  variables: child<Variable, VariableInput>('variables'),
  lore: child<Lore, LoreInput>('lore'),
  events: child<StoryEvent, EventInput>('events'),

  problems: (storyId: number) => request<Problems>(`/stories/${storyId}/problems`),
  playtest: (storyId: number, body: { runs?: number; max_steps?: number; seed?: number | null }) =>
    post<Playtest>(`/stories/${storyId}/playtest`, body),
  // a download link, not a fetch: the browser saves the Twee file
  tweeUrl: (storyId: number) => `/api/stories/${storyId}/export.twee`,
  play: {
    start: (storyId: number, passageId: number | null = null) =>
      post<PlayView>(`/stories/${storyId}/play/start`, { passage_id: passageId }),
    choose: (storyId: number, choiceId: number, state: PlayState) =>
      post<PlayView>(`/stories/${storyId}/play/choose`, { choice_id: choiceId, state }),
  },

  assist: (storyId: number, passageId: number, body: AssistRequest) =>
    post<Proposal[]>(`/stories/${storyId}/passages/${passageId}/assist`, body),
  proposals: {
    accept: (storyId: number, id: number) => post<Proposal>(`/stories/${storyId}/proposals/${id}/accept`),
    reject: (storyId: number, id: number) => post<Proposal>(`/stories/${storyId}/proposals/${id}/reject`),
  },

  runs: {
    list: (storyId: number) => request<AgentRun[]>(`/stories/${storyId}/runs`),
    start: (storyId: number, body: RunCreate) => post<AgentRun>(`/stories/${storyId}/runs`, body),
    get: (storyId: number, id: number) => request<AgentRun>(`/stories/${storyId}/runs/${id}`),
    resume: (storyId: number, id: number, body: { steer?: string | null; stop?: boolean }) =>
      post<AgentRun>(`/stories/${storyId}/runs/${id}/resume`, body),
    cancel: (storyId: number, id: number) => post<AgentRun>(`/stories/${storyId}/runs/${id}/cancel`),
    state: (storyId: number, id: number) => request<RunState>(`/stories/${storyId}/runs/${id}/state`),
    // The event stream is SSE; the caller owns the EventSource.
    events: (storyId: number, id: number, after = 0) => new EventSource(`/api/stories/${storyId}/runs/${id}/events?after=${after}`),
  },
  workflows: () => request<Record<string, WorkflowGraph>>('/agents/workflows'),
  pacing: {
    get: (storyId: number) => request<PacingPlan>(`/stories/${storyId}/pacing`),
    save: (storyId: number, plan: PacingPlan) =>
      request<PacingPlan>(`/stories/${storyId}/pacing`, { method: 'PUT', body: JSON.stringify(plan) }),
    reset: (storyId: number) => request<PacingPlan>(`/stories/${storyId}/pacing`, { method: 'DELETE' }),
  },
  prompts: {
    list: () => request<PromptInfo[]>('/agents/prompts'),
    save: (key: string, body: PromptUpdate) =>
      request<PromptInfo>(`/agents/prompts/${key}`, { method: 'PUT', body: JSON.stringify(body) }),
    reset: (key: string) => request<PromptInfo>(`/agents/prompts/${key}`, { method: 'DELETE' }),
    // the story only supplies the passage the context frame is built from
    preview: (storyId: number, key: string, body: PromptPreviewRequest) =>
      post<{ prompt: string }>(`/stories/${storyId}/prompts/${key}/preview`, body),
  },

  llmModels: () => request<string[]>('/llm/models'),
}
