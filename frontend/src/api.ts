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

export interface PassageMeta {
  location?: string
  present?: string[]
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
}

export type LoreKind = 'character' | 'place' | 'faction' | 'rule' | 'fact'
export const LORE_KINDS: LoreKind[] = ['character', 'place', 'faction', 'rule', 'fact']

export interface Lore {
  id: number
  story_id: number
  kind: LoreKind
  name: string
  body: string
  reviewed: boolean
}

// --- proposals (agent output; only Accept writes to the tables above) ---------

export type ProposalKind = 'new_passage' | 'new_choice' | 'edit_passage' | 'edit_choice' | 'note'
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
    issues?: { claim: string; conflicts_with: string; severity: 'minor' | 'major' }[]
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

// expand_frontier's draft/audit/revise loop (agents.DEFAULT_REFINE): `rounds` is the most
// revisions a passage gets; 0 skips the loop.
export interface RefinePolicy {
  rounds?: number
  votes?: number
  audit_reasoning?: boolean
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
  endings?: number // passages written as endings: terminal leaves
  open?: number // passages written but not yet grown: non-terminal leaves
  checked?: number
  retries?: number
  rounds?: number
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

// One saved run event; a retry carries why the answer was refused and what it said.
export interface RunLogEvent {
  type: string
  seq: number
  at: number
  passage_id?: number | null
  phase?: string | null
  attempt?: number
  kind?: 'refused' | 'exception'
  seconds?: number
  reason?: string
  answer?: string | null
  schema?: string
  model?: string
  [k: string]: unknown
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

// --- interviews (Interview Me; see concepts/interview-me.md) ----------------------

export type InterviewMode = 'answer' | 'skip' | 'decide'

// One write an agent turn made: before null is a create, after null a delete.
export interface InterviewWrite {
  table: 'stories' | 'lore'
  id: number
  before: Record<string, unknown> | null
  after: Record<string, unknown> | null
  reverted: boolean
}

export interface InterviewEntry {
  role: 'agent' | 'author'
  text: string
  mode: InterviewMode | null // author entries
  writes: InterviewWrite[] // agent entries
  opened: number[] // conflict ids this turn opened
  resolved: number[] // conflict ids this turn resolved
}

// Two statements that cannot both hold; only the author's answer resolves one.
export interface InterviewConflict {
  id: number
  about: string
  a: string
  b: string
  source: 'model' | 'code'
  status: 'open' | 'resolved'
  resolution: string | null
}

export interface Interview {
  id: number
  story_id: number
  status: 'open' | 'done'
  transcript: InterviewEntry[]
  conflicts: InterviewConflict[]
  created_at: string
  updated_at: string
}

export interface InterviewTurnRequest {
  text?: string
  mode?: InterviewMode
  model?: string | null
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
  lore: Lore[]
  proposals: Proposal[] // pending only
}

// --- play & validation -------------------------------------------------------

export interface PlayView {
  passage: Passage
  choices: Choice[]
}

export interface Problems {
  no_start: boolean
  unreachable: number[]
  dangling: number[]
  endings: number[] // is_ending passages: decided, terminal
  unfinished: number[] // no outgoing choices, but not decided as an ending
  merge_candidates: MergeCandidate[]
}

// Passages sharing story time and location: one scene reached by several routes.
export interface MergeCandidate {
  t: number
  location: string
  passage_ids: number[]
}

// What pruning a passage deletes: every passage below it the start cannot reach another way
// (not the passage itself) and every choice out of it or them.
export interface PruneResult {
  passage_ids: number[]
  choice_ids: number[]
}

// Map positions written in one go: the map's auto layout, kept (POST .../layout).
export interface PassagePosition {
  id: number
  pos_x: number
  pos_y: number
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
  dangling: Record<string, number>
}

type Omitted = 'id' | 'story_id'
export type PassageInput = Partial<Omit<Passage, Omitted>> & { title: string }
export type ChoiceInput = Partial<Omit<Choice, Omitted>> & { from_passage: number; text: string }
export type LoreInput = Partial<Omit<Lore, Omitted>> & { kind: LoreKind; name: string }

// Machine settings (models.SettingsInfo / SettingsPatch): the default model and the number of model calls in flight.
export type SettingsInfo = {
  saved: { model?: string; concurrency?: number }
  model: string // in effect when a request names none
  concurrency: number // the gate's size now
  backend: string
  url: string
  env_model: string // what clearing the saved model falls back to
  env_concurrency: number
}
export type SettingsPatch = { model?: string | null; concurrency?: number | null }

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
    // delete the passage's choices and everything below them to the leaves, leaving it a frontier
    prune: (storyId: number, id: number) => post<PruneResult>(`/stories/${storyId}/passages/${id}/prune`),
    // what prune would delete; changes nothing
    prunePreview: (storyId: number, id: number) => request<PruneResult>(`/stories/${storyId}/passages/${id}/prune`),
    // fold `passageIds` into `into`: inbound choices retargeted, outgoing moved, the others deleted
    merge: (storyId: number, into: number, passageIds: number[]) =>
      post<Passage>(`/stories/${storyId}/passages/merge`, { into, passage_ids: passageIds }),
  },
  // returns the ids it moved; ids from another story or already deleted are skipped
  layout: (storyId: number, positions: PassagePosition[]) =>
    post<{ passage_ids: number[] }>(`/stories/${storyId}/layout`, { positions }),
  promote: (storyId: number, body: PromoteRequest) => post<{ passage_ids: number[] }>(`/stories/${storyId}/promote`, body),
  choices: child<Choice, ChoiceInput>('choices'),
  lore: child<Lore, LoreInput>('lore'),

  problems: (storyId: number) => request<Problems>(`/stories/${storyId}/problems`),
  playtest: (storyId: number, body: { runs?: number; max_steps?: number; seed?: number | null }) =>
    post<Playtest>(`/stories/${storyId}/playtest`, body),
  // a download link, not a fetch: the browser saves the Twee file
  tweeUrl: (storyId: number) => `/api/stories/${storyId}/export.twee`,
  play: {
    start: (storyId: number, passageId: number | null = null) =>
      post<PlayView>(`/stories/${storyId}/play/start`, { passage_id: passageId }),
    choose: (storyId: number, choiceId: number) =>
      post<PlayView>(`/stories/${storyId}/play/choose`, { choice_id: choiceId }),
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
    log: (storyId: number, id: number, type?: string) =>
      request<RunLogEvent[]>(`/stories/${storyId}/runs/${id}/log${type ? `?type=${type}` : ''}`),
    clear: (storyId: number) => request<{ cleared: number }>(`/stories/${storyId}/runs`, { method: 'DELETE' }),
    get: (storyId: number, id: number) => request<AgentRun>(`/stories/${storyId}/runs/${id}`),
    resume: (storyId: number, id: number, body: { steer?: string | null; stop?: boolean }) =>
      post<AgentRun>(`/stories/${storyId}/runs/${id}/resume`, body),
    cancel: (storyId: number, id: number) => post<AgentRun>(`/stories/${storyId}/runs/${id}/cancel`),
    state: (storyId: number, id: number) => request<RunState>(`/stories/${storyId}/runs/${id}/state`),
    // The event stream is SSE; the caller owns the EventSource.
    events: (storyId: number, id: number, after = 0) => new EventSource(`/api/stories/${storyId}/runs/${id}/events?after=${after}`),
  },
  interviews: {
    list: (storyId: number) => request<Interview[]>(`/stories/${storyId}/interviews`),
    start: (storyId: number, body: { model?: string | null } = {}) => post<Interview>(`/stories/${storyId}/interviews`, body),
    get: (storyId: number, id: number) => request<Interview>(`/stories/${storyId}/interviews/${id}`),
    turn: (storyId: number, id: number, body: InterviewTurnRequest) =>
      post<Interview>(`/stories/${storyId}/interviews/${id}/turn`, body),
    revert: (storyId: number, id: number, body: { entry: number; write?: number | null }) =>
      post<Interview>(`/stories/${storyId}/interviews/${id}/revert`, body),
    close: (storyId: number, id: number) => post<Interview>(`/stories/${storyId}/interviews/${id}/close`),
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

  settings: {
    get: () => request<SettingsInfo>('/settings'),
    save: (body: SettingsPatch) => request<SettingsInfo>('/settings', { method: 'PUT', body: JSON.stringify(body) }),
  },
}
