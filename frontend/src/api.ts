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
  events?: number[]
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
export type AssistVerb = 'draft_body' | 'suggest_choices' | 'draft_target' | 'rewrite'

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

export interface RunCreate {
  workflow: Workflow
  scope: number[]
  model?: string | null
  steer?: string | null
  budget?: { max_passages?: number; depth?: number; max_seconds?: number }
}

export interface AgentRun {
  id: number
  story_id: number
  workflow: Workflow
  thread_id: string
  params: { model?: string | null; steer?: string | null }
  scope: number[]
  budget: { max_passages?: number; depth?: number; max_seconds?: number }
  status: RunStatus
  progress: { level?: number; created?: number; frontier?: number; checked?: number }
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
  | { type: 'progress'; level?: number; created?: number; frontier?: number; checked?: number }
  | { type: 'interrupt'; value: { level: number; created: number; next: number } }
  | { type: 'status'; status: RunStatus; error: string | null }
)

export interface RunState {
  values: Record<string, unknown>
  next: string[]
  history: { step: number | null; next: string[]; created_at: string | null }[]
}

// compiled.get_graph().to_json()
export interface WorkflowGraph {
  nodes: { id: string; type?: string; data?: unknown }[]
  edges: { source: string; target: string; conditional?: boolean }[]
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
  endings: number[]
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
  },
  choices: child<Choice, ChoiceInput>('choices'),
  variables: child<Variable, VariableInput>('variables'),
  lore: child<Lore, LoreInput>('lore'),
  events: child<StoryEvent, EventInput>('events'),

  problems: (storyId: number) => request<Problems>(`/stories/${storyId}/problems`),
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

  llmModels: () => request<string[]>('/llm/models'),
}
