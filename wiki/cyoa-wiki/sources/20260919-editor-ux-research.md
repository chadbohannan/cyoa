# Web research: UI patterns in existing branching-story tools (2026-09-19)

Raw notes gathered to scope the local webapp UI (create / edit / play).

## Twine 2 (twinery.org/reference)
- Story library screen (cards per story) → story map screen (passages as cards on an infinite canvas; arrows for links; broken links drawn as a dashed arrow to nowhere).
- Passage edit: double-click a card → dialog; up to five edit dialogs stack (most recent on top). Fields: name (␣ shown for leading/trailing spaces), tags (colored), text with story-format syntax highlighting; toolbar: Tag, Rename, Size (card size), Start Story Here, undo/redo. Autosaves on typing pause.
- Typing `[[Link]]` auto-creates the missing passage as an empty card; deleting the link removes the auto-created passage unless it was edited/tagged/linked elsewhere.
- Story-level: Details (IFID, format), Find & Replace, highlight passages by tag / search, story JavaScript & stylesheet, Proof (readable dump), Test (play with debug), Play, Publish to File. SugarCube adds "Test From Here". 2.9 added `data-tag` on cards; 2.10 (2024-11) moved story-format list to a dialog, pref to disable enhanced editor.

## Inky (inkle) — github.com/inkle/inky
- Two panes: ink source on the left, live player on the right. Player recompiles on edit and fast-forwards through the previously chosen choices so you stay where you were. Rewind one choice / restart. Issue browser (errors, warnings, TODOs) with jump-to-line. Word count. Alt-click a divert to jump to its target. Export to web.

## Arcweave (docs.arcweave.com)
- Boards (multiple canvases per project) of Elements (title bar + content), Connections (labelled arrows = choices), Branches (condition nodes that pick a connection), Jumpers (cross-board links), Components (reusable entities), global Variables, arcscript in element text.
- Play Mode: one element at a time, options as buttons labelled by connection label (falling back to target title), Back, Restart (resets variables and visits), "Quickstart Play Mode from here" on any element, in-play text editing, Debugger panel showing current board/element and variable changes.

## Yarn Spinner editor (VS Code) / try.yarnspinner.dev
- Node cards preview the first lines of dialogue. Preview runs the real runtime inside the editor; pick options, watch variables change, restart at any node. Export a self-contained playable HTML.

## CSIDE (ChoiceScript IDE) — choicescriptide.github.io
- Code editor + side-by-side game run. Integrated Quicktest (walk every #option and both sides of every *if) and Randomtest (10,000 random playthroughs, with line-coverage stats). Errors highlight the offending line.

## LLM-assisted writing tools (AI Dungeon, NovelAI, Sudowrite, KoboldAI, SillyTavern)
- Common patterns: "continue"/"expand" from the cursor, regenerate/reroll, a "story bible"/lorebook of world facts injected into every prompt, memory of recent plot, adjustable tone/style presets. None of them expose branching structure as a first-class graph; the branch is implicit in the chat log.
