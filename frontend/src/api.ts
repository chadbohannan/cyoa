// Typed gateway to the FastAPI backend (proxied at /api by Vite in dev).

export interface Story {
  id: number
  title: string
  description: string
  created_at: string
}

export interface Passage {
  id: number
  story_id: number
  title: string
  body: string
  is_start: boolean
  pos_x: number
  pos_y: number
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

export interface Variable {
  id: number
  story_id: number
  name: string
  kind: 'flag' | 'stat' | 'item'
  default: unknown
}

export interface StoryGraph {
  story: Story
  passages: Passage[]
  choices: Choice[]
  variables: Variable[]
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`/api${path}`, {
    headers: { 'Content-Type': 'application/json' },
    ...init,
  })
  if (!res.ok) throw new Error(`${res.status} ${await res.text()}`)
  return res.status === 204 ? (undefined as T) : res.json()
}

export const api = {
  health: () => request<{ ok: boolean }>('/health'),
  stories: () => request<Story[]>('/stories'),
  createStory: (title: string) =>
    request<Story>('/stories', { method: 'POST', body: JSON.stringify({ title }) }),
  graph: (storyId: number) => request<StoryGraph>(`/stories/${storyId}/graph`),
  createPassage: (storyId: number, p: Partial<Passage> & { title: string }) =>
    request<Passage>(`/stories/${storyId}/passages`, { method: 'POST', body: JSON.stringify(p) }),
  createChoice: (storyId: number, c: Partial<Choice> & { from_passage: number; text: string }) =>
    request<Choice>(`/stories/${storyId}/choices`, { method: 'POST', body: JSON.stringify(c) }),
}
