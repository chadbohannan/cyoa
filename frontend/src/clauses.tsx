// Clause-row editors for a choice's condition and effects. They edit the JSON
// vocabulary owned by backend/src/cyoa/engine.py one row at a time: a condition
// is a flat list of {var, op, value} clauses joined by all/any, and effects are
// rows of set / add / give / take. Shapes the rows cannot express (nested
// combinators, unknown keys) fall back to the raw JSON field so nothing is lost.
//
// Each editor is parse → rows → build: parse the stored JSON into rows (or null
// when it cannot), render one row component per entry, and write the whole
// object back on every change.

import { useState } from 'react'
import type { Choice, Variable, VariableKind } from './api'
import { JsonField } from './fields'

type Json = Record<string, unknown>
type EditorProps = { choice: Choice; variables: Variable[]; onSave: (v: Json) => void }

const OPS = ['==', '!=', '<', '<=', '>', '>='] as const
type Op = (typeof OPS)[number]

// --- condition ---------------------------------------------------------------

type Clause = { var: string; op: Op; value: unknown }
type Mode = 'all' | 'any'

const isClause = (x: unknown): x is Clause =>
  typeof x === 'object' && x !== null && typeof (x as Clause).var === 'string' && OPS.includes((x as Clause).op)

// null when the condition is not a flat all/any of clauses
function parseCondition(cond: Json): { mode: Mode; clauses: Clause[] } | null {
  const keys = Object.keys(cond)
  if (keys.length === 0) return { mode: 'all', clauses: [] }
  if (isClause(cond)) return { mode: 'all', clauses: [cond] }
  if (keys.length === 1 && (keys[0] === 'all' || keys[0] === 'any')) {
    const list = cond[keys[0]]
    if (Array.isArray(list) && list.every(isClause)) return { mode: keys[0], clauses: list }
  }
  return null
}

function buildCondition(mode: Mode, clauses: Clause[]): Json {
  if (clauses.length === 0) return {}
  if (clauses.length === 1) return { ...clauses[0] }
  return { [mode]: clauses.map((c) => ({ ...c })) }
}

export function ConditionEditor({ choice, variables, onSave }: EditorProps) {
  const parsed = parseCondition(choice.condition)
  if (!parsed) return <Fallback label="condition" value={choice.condition} onSave={onSave} />
  const { mode, clauses } = parsed
  const save = (m: Mode, cs: Clause[]) => onSave(buildCondition(m, cs))
  const add = () => save(mode, [...clauses, { var: firstVar(variables), op: '==', value: defaultFor(variables[0]?.kind) }])

  return (
    <div className="clauses">
      {clauses.length === 0 && <p className="muted small">Always available.</p>}
      {clauses.length > 1 && (
        <select value={mode} onChange={(e) => save(e.target.value as Mode, clauses)} className="narrow">
          <option value="all">all of</option>
          <option value="any">any of</option>
        </select>
      )}
      {clauses.map((c, i) => (
        <ClauseRow
          key={i}
          clause={c}
          variables={variables}
          onChange={(next) => save(mode, replaceAt(clauses, i, next))}
          onRemove={() => save(mode, clauses.filter((_, j) => j !== i))}
        />
      ))}
      <div className="row">
        <button onClick={add}>+ Clause</button>
        <RawToggle value={choice.condition} onSave={onSave} />
      </div>
    </div>
  )
}

function ClauseRow({ clause: c, variables, onChange, onRemove }: RowProps<Clause>) {
  const kind = kindOf(variables, c.var)
  return (
    <div className="row clause">
      <VarSelect variables={variables} value={c.var} onChange={(v) => onChange({ ...c, var: v, value: coerce(kindOf(variables, v), c.value) })} />
      <select value={c.op} onChange={(e) => onChange({ ...c, op: e.target.value as Op })} className="narrow">
        {OPS.map((o) => <option key={o}>{o}</option>)}
      </select>
      <ValueInput kind={kind} value={c.value} onChange={(value) => onChange({ ...c, value })} />
      <RemoveButton onClick={onRemove} />
    </div>
  )
}

// --- effects -----------------------------------------------------------------

const ACTIONS = ['set', 'add', 'give', 'take'] as const
type Action = (typeof ACTIONS)[number]
type EffectRow = { action: Action; var: string; value?: unknown }
const takesValue = (a: Action) => a === 'set' || a === 'add'

// null when effects has a key or shape the rows cannot show
function parseEffects(eff: Json): EffectRow[] | null {
  const rows: EffectRow[] = []
  for (const [key, v] of Object.entries(eff)) {
    if ((key === 'set' || key === 'add') && typeof v === 'object' && v !== null && !Array.isArray(v)) {
      for (const [name, value] of Object.entries(v as Json)) rows.push({ action: key, var: name, value })
    } else if ((key === 'give' || key === 'take') && Array.isArray(v) && v.every((x) => typeof x === 'string')) {
      for (const name of v as string[]) rows.push({ action: key, var: name })
    } else {
      return null
    }
  }
  return rows
}

function buildEffects(rows: EffectRow[]): Json {
  const out: { set?: Json; add?: Json; give?: string[]; take?: string[] } = {}
  for (const r of rows) {
    if (!r.var) continue
    if (r.action === 'set' || r.action === 'add') (out[r.action] ??= {})[r.var] = r.value
    else (out[r.action] ??= []).push(r.var)
  }
  return out
}

export function EffectsEditor({ choice, variables, onSave }: EditorProps) {
  const rows = parseEffects(choice.effects)
  if (!rows) return <Fallback label="effects" value={choice.effects} onSave={onSave} />
  const save = (rs: EffectRow[]) => onSave(buildEffects(rs))
  const add = () => save([...rows, { action: 'set', var: firstVar(variables), value: defaultFor(variables[0]?.kind) }])

  return (
    <div className="clauses">
      {rows.length === 0 && <p className="muted small">No effects.</p>}
      {rows.map((r, i) => (
        <EffectRowEditor
          key={i}
          clause={r}
          variables={variables}
          onChange={(next) => save(replaceAt(rows, i, next))}
          onRemove={() => save(rows.filter((_, j) => j !== i))}
        />
      ))}
      <div className="row">
        <button onClick={add}>+ Effect</button>
        <RawToggle value={choice.effects} onSave={onSave} />
      </div>
    </div>
  )
}

function EffectRowEditor({ clause: r, variables, onChange, onRemove }: RowProps<EffectRow>) {
  const kind = kindOf(variables, r.var)
  const setAction = (action: Action) =>
    onChange(takesValue(action) ? { action, var: r.var, value: r.value ?? defaultFor(kind) } : { action, var: r.var })
  const setVar = (v: string) =>
    onChange({ ...r, var: v, value: takesValue(r.action) ? coerce(kindOf(variables, v), r.value) : undefined })
  return (
    <div className="row clause">
      <select value={r.action} onChange={(e) => setAction(e.target.value as Action)} className="narrow">
        {ACTIONS.map((a) => <option key={a}>{a}</option>)}
      </select>
      <VarSelect variables={variables} value={r.var} onChange={setVar} />
      {takesValue(r.action) && (
        // `add` is arithmetic even on a flag, so it always takes a number
        <ValueInput kind={r.action === 'add' ? 'stat' : kind} value={r.value} onChange={(value) => onChange({ ...r, value })} />
      )}
      <RemoveButton onClick={onRemove} />
    </div>
  )
}

// --- shared pieces -----------------------------------------------------------

type RowProps<T> = { clause: T; variables: Variable[]; onChange: (next: T) => void; onRemove: () => void }

const replaceAt = <T,>(xs: T[], i: number, x: T) => xs.map((y, j) => (j === i ? x : y))
const firstVar = (variables: Variable[]) => variables[0]?.name ?? ''
const kindOf = (variables: Variable[], name: string): VariableKind | undefined => variables.find((v) => v.name === name)?.kind
const defaultFor = (kind: VariableKind | undefined) => (kind === 'flag' ? true : 0)
// keep a value sensible when the variable under it changes kind
function coerce(kind: VariableKind | undefined, value: unknown) {
  if (kind === 'flag') return typeof value === 'boolean' ? value : true
  return typeof value === 'number' ? value : 0
}

function VarSelect({ variables, value, onChange }: { variables: Variable[]; value: string; onChange: (v: string) => void }) {
  const declared = variables.some((v) => v.name === value)
  return (
    <select value={value} onChange={(e) => onChange(e.target.value)} className={declared ? 'grow' : 'grow error'} title={declared ? undefined : 'undeclared variable'}>
      {!declared && <option value={value}>{value || '—'} (undeclared)</option>}
      {variables.map((v) => <option key={v.id} value={v.name}>{v.name}</option>)}
    </select>
  )
}

function ValueInput({ kind, value, onChange }: { kind: VariableKind | undefined; value: unknown; onChange: (v: unknown) => void }) {
  if (kind === 'flag') {
    return (
      <select value={String(value === true)} onChange={(e) => onChange(e.target.value === 'true')} className="narrow">
        <option value="true">true</option>
        <option value="false">false</option>
      </select>
    )
  }
  return (
    <input type="number" className="narrow" value={typeof value === 'number' ? value : ''} onChange={(e) => onChange(e.target.value === '' ? 0 : Number(e.target.value))} />
  )
}

const RemoveButton = ({ onClick }: { onClick: () => void }) => (
  <button className="icon danger" title="remove" onClick={onClick}>×</button>
)

function Fallback({ label, value, onSave }: { label: string; value: Json; onSave: (v: Json) => void }) {
  return (
    <div>
      <p className="muted small">This {label} is more complex than the row editor handles; edit it as JSON.</p>
      <JsonField value={value} onSave={(v) => onSave(v as Json)} />
    </div>
  )
}

function RawToggle({ value, onSave }: { value: Json; onSave: (v: Json) => void }) {
  const [open, setOpen] = useState(false)
  return (
    <>
      <button className="link small" onClick={() => setOpen((o) => !o)}>{open ? 'hide JSON' : 'JSON'}</button>
      {open && <div className="raw"><JsonField value={value} onSave={(v) => onSave(v as Json)} /></div>}
    </>
  )
}
