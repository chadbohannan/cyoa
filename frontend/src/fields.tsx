// Autosaving form controls. Each keeps a local draft, saves it after a typing
// pause or on blur, and re-syncs when the underlying row changes (e.g. the
// selection moves). There is no Save button anywhere in the editor.

import { useEffect, useRef, useState, type ComponentProps } from 'react'

const PAUSE_MS = 600

function useDraft<T>(value: T, onSave: (v: T) => void) {
  const [draft, setDraft] = useState(value)
  const dirty = useRef(false)
  const timer = useRef<number | undefined>(undefined)
  const latest = useRef({ draft, onSave })
  useEffect(() => {
    latest.current = { draft, onSave }
  })

  useEffect(() => {
    if (!dirty.current) setDraft(value)
  }, [value])

  const commit = () => {
    window.clearTimeout(timer.current)
    if (!dirty.current) return
    dirty.current = false
    latest.current.onSave(latest.current.draft)
  }
  const edit = (v: T) => {
    dirty.current = true
    setDraft(v)
    window.clearTimeout(timer.current)
    timer.current = window.setTimeout(commit, PAUSE_MS)
  }
  // flush a pending edit if the control unmounts mid-pause
  useEffect(() => () => commit(), []) // eslint-disable-line react-hooks/exhaustive-deps
  return { draft, edit, commit }
}

type Base = { value: string; onSave: (v: string) => void }

export function TextField({ value, onSave, ...rest }: Base & Omit<ComponentProps<'input'>, 'value' | 'onChange'>) {
  const { draft, edit, commit } = useDraft(value, onSave)
  return <input {...rest} value={draft} onChange={(e) => edit(e.target.value)} onBlur={commit} />
}

export function TextArea({ value, onSave, ...rest }: Base & Omit<ComponentProps<'textarea'>, 'value' | 'onChange'>) {
  const { draft, edit, commit } = useDraft(value, onSave)
  return <textarea {...rest} value={draft} onChange={(e) => edit(e.target.value)} onBlur={commit} />
}

// Numeric input where empty means null.
export function NumberField({
  value,
  onSave,
  ...rest
}: { value: number | null; onSave: (v: number | null) => void } & Omit<ComponentProps<'input'>, 'value' | 'onChange' | 'type'>) {
  const { draft, edit, commit } = useDraft(value === null ? '' : String(value), (s) =>
    onSave(s.trim() === '' ? null : Number(s)),
  )
  return <input {...rest} type="number" value={draft} onChange={(e) => edit(e.target.value)} onBlur={commit} />
}

// JSON textarea; saves only when the draft parses, and shows why when it does not.
export function JsonField({ value, onSave, rows = 4 }: { value: unknown; onSave: (v: unknown) => void; rows?: number }) {
  const [err, setErr] = useState<string | null>(null)
  const { draft, edit, commit } = useDraft(JSON.stringify(value, null, 1), (s) => {
    try {
      onSave(JSON.parse(s))
      setErr(null)
    } catch (e) {
      setErr((e as Error).message)
    }
  })
  return (
    <div>
      <textarea className="mono" rows={rows} value={draft} onChange={(e) => edit(e.target.value)} onBlur={commit} />
      {err && <div className="error small">{err}</div>}
    </div>
  )
}
