# Frontend

The frontend is a Vite + React + TypeScript app in `frontend/`, started with `make frontend` on port 5173. Its `vite.config.ts` proxies `/api` to the [backend](backend.md) at `CYOA_API` (default `http://127.0.0.1:8765`), so the app code calls relative `/api/...` paths and never knows the backend's address.

`src/api.ts` is the typed gateway: the `Story`, `Passage`, `Choice`, `Variable`, and `StoryGraph` interfaces mirror the backend's Pydantic models field for field, and the `api` object wraps the handful of routes that exist. Keeping every fetch behind this one module is what will let the [story model](../concepts/story-model.md) change shape without a hunt through components.

## The graph editor

The bootstrap interview (`20260919-bootstrap-interview.md`) chose a graph editor as the first UI rather than a reader, and React Flow (`@xyflow/react`) as its library. `src/StoryCanvas.tsx` renders a `StoryGraph` as a React Flow canvas: each passage becomes a node positioned by the passage's `pos_x`/`pos_y` (the backend stores these purely as a layout hint for the editor — the engine will ignore them), the start passage gets React Flow's `input` node type and a ▶ marker, and each choice with a target becomes a labeled edge. Choices whose `to_passage` is null (dangling, not yet wired) are simply not drawn yet, which is a known gap.

As of 2026-09-19 the canvas is read-only: nodes can be dragged but positions are not written back, and nothing creates passages or choices from the UI. `src/App.tsx` provides only a story picker and a "New story" button. Editing lands once the model settles; the point of the skeleton was to prove the backend-to-canvas round trip.

`make test-frontend` typechecks (`tsc -b`) and builds; there is no test runner yet.

The planned evolution of this surface into a full create/edit/play app is scoped in [the webapp UI](../concepts/webapp-ui.md).

| Reference | Role |
|-----------|------|
| `frontend/vite.config.ts` | dev server and `/api` proxy |
| `frontend/src/api.ts` | typed backend gateway |
| `frontend/src/StoryCanvas.tsx` | React Flow rendering of a story |
| `frontend/src/App.tsx` | story picker shell |
