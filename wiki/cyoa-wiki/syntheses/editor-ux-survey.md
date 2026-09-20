# What Existing Branching-Story Editors Get Right

This synthesis distills the [UX research notes](../sources/20260919-editor-ux-research.md) into the handful of patterns worth copying for our own [webapp UI](../concepts/webapp-ui.md). Five tools were surveyed: Twine 2 (the closest cousin — a graph of passages), Inky (text with a live player), Arcweave (a commercial board editor with a debuggable play mode), the Yarn Spinner editor (node cards with an embedded runtime), and CSIDE (ChoiceScript with automated test runs). AI writing tools (AI Dungeon, NovelAI, Sudowrite, KoboldAI) were looked at for the LLM side.

## The map is the document

Twine, Arcweave and Yarn Spinner all make the canvas the primary surface: you never see a passage list without seeing where it sits. Cards show enough text to be recognizable from a distance (Yarn Spinner previews the first lines; Arcweave gives each element a colored title bar and content body), and links are drawn as arrows so structure is legible at a glance. Twine's cheapest, most-loved trick is that writing `[[a link]]` in a passage *creates* the target card — authors extend the graph by writing, not by dragging out nodes. Our React Flow [frontend](../systems/frontend.md) already renders this map; the survey says the map should also be where creation happens.

## Edit in place, never leave the map

Twine opens passage editing in a dialog over the map and lets up to five stack so you can hop between neighboring passages. Arcweave edits in a side panel. Both keep the canvas visible so the author never loses their place. The fields are the same everywhere: a name, a body, tags, and a way to mark the start passage. Autosave on a typing pause is universal; there is no Save button anywhere in the surveyed tools.

## Play is a mode of the editor, not a separate app

Every tool has a play surface a click away, and the good ones are *tied to the editor state*: Inky recompiles on every keystroke and fast-forwards through the choices you already made so you stay at the same point in the story; Arcweave and Twine's SugarCube offer "play from here" on any node; Yarn Spinner lets you restart at any node. The player shows one passage at a time with choices as buttons, and always has Back and Restart. The serious ones add a debugger: Arcweave's panel shows the current element and every variable change, Yarn Spinner shows variables ticking as you choose. For a tool whose model has conditions and effects, this debugger is what makes those features testable.

## Validation and automated play

CSIDE's Quicktest (exhaustively walk every option) and Randomtest (10,000 random playthroughs with coverage) are the only surveyed features that treat the story as a testable program; Inky's issue browser and Twine's dashed "broken link" arrows are the lightweight version. Combined with the graph metrics from the [community landscape](community-landscape.md) (endings, unreachable passages, longest path), this suggests a "story health" panel is cheap and distinctive.

## LLM assistance is cursor-local and context-fed

None of the AI writing tools model branching as a graph; they generate from a chat log. What they do well is the interaction grammar: continue from here, expand this, regenerate, and a "story bible" of world facts injected into every prompt. Translated to a graph editor, the natural verbs are *draft this passage*, *suggest N choices from this passage*, *draft the passage a dangling choice points to*, and *rewrite in the story's voice*, each with the story description and neighboring passages as context. That is a thin layer over the [backend's](../systems/backend.md) existing `/api/llm/generate`.

## Reference

| Pattern | Seen in | Adopt? |
| --- | --- | --- |
| Canvas as primary surface, cards preview text | Twine, Arcweave, Yarn Spinner | Yes |
| Writing a link creates the target passage | Twine | Yes (as a "+ choice → new passage" action and `[[link]]` parsing later) |
| Edit dialog/panel over the map, autosave | Twine, Arcweave | Yes — side panel |
| Play from here, Back, Restart | Arcweave, SugarCube, Yarn Spinner | Yes |
| Live player fast-forwards after edits | Inky | Later |
| Variable debugger in play mode | Arcweave, Yarn Spinner | Yes |
| Broken-link / unreachable markers | Twine, Inky | Yes |
| Exhaustive / random automated playthroughs | CSIDE | Later |
| Story bible + continue/expand/regenerate | AI writing tools | Yes, as per-passage LLM actions |
