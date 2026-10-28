# Interchange Formats

The [story model](story-model.md) stores a story as rows in SQLite, which is right for an editor but useless for sharing. The community has three established text formats and one JSON shape that our tables map onto almost directly; this page records the mapping so that an import or export can be added without redesigning anything. The survey behind it is in the [community landscape](../syntheses/community-landscape.md) synthesis and the [research notes](../sources/20260919-community-research.md).

## Why Twee 3 is the natural first target

Twee 3 is a plain-text serialization of a Twine story: each passage is a `:: Name [tags] {"position":"x,y"}` header followed by prose, and links are `[[Text->Target]]` inside the prose. A `StoryData` passage holds JSON with an `ifid`, the `start` passage name and the story format. Every field there has a home in our tables: `passages.title` is the name, `passages.body` is the prose, `pos_x`/`pos_y` are the `position` metadata, `is_start` is `StoryData.start`, and each `choices` row is one `[[link]]`. Twee is also the format Twine itself imports, and Tweego compiles it, so an export gives an author a playable HTML book for free. The gap is that Twee has no native notion of conditions or effects — those live in story-format markup (Harlowe, SugarCube) — so on export our `condition`/`effects` JSON would have to be rendered as SugarCube `<<if>>`/`<<set>>` macros or dropped, and on import they cannot be recovered. The export exists as of 2026-09-20 (`backend/src/cyoa/twee.py`, described on the [backend](../systems/backend.md) page) and takes the macro route: gated links are wrapped in `<<if>>`, choices with effects become `<<link>>` blocks containing `<<set>>`, and the variables table becomes a `StoryInit` passage.

The Twison JSON shape (`passages[].links[] = {name, link, pid}`) is the same data with links pre-parsed; it is worth accepting on import because Twine users can produce it with one click.

## What ink teaches about conditions and effects

ink is the richest open language for branching logic: choices are once-only (`*`) or sticky (`+`), can be gated by `{condition}`, and the story has global `VAR`s, temporary variables, `LIST` sets and `visited` counts. Our `choices.condition` and `choices.effects` JSON columns were introduced precisely so this vocabulary could grow, and ink is the checklist for what it eventually needs: at minimum a comparison on a stat, a flag test, an inventory test, a "visited" test, and set/add/remove effects. Compiling to ink's runtime JSON is possible but the format is a bytecode-like container, not a friendly target; if we ever export to ink it should be to the `.ink` text form.

## Adopt IFIDs now

The Treaty of Babel assigns every interactive work a UUID-shaped IFID, and Twine writes it into `StoryData`. Adding an `ifid` column to `stories` (generated with `uuid4()` on create) costs nothing today and means any future export is already a well-formed Twine or iFiction record. This is the one schema change this research recommends; per the no-migrations rule in the [dev loop](../runbooks/dev-loop.md) it means editing `db.py` and deleting `cyoa.db`.

## Formats to treat as references only

ChoiceScript's stat-driven `*choice`/`*set`/`*if` is the commercial standard, but its license is non-commercial-only, so we borrow ideas (stat charts, fairness of stat tests) rather than code. Project Aon's XML (`<section id>` with `<choice idref>` plus separate mechanics files) is the best-documented encoding of a numbered-section gamebook and a useful model for a future "print layout" that shuffles passages into numbered sections, but its content license prevents using the data.

## Reference

| Our table/column | Twee 3 | Twison JSON | ink |
| --- | --- | --- | --- |
| `stories.title` | `:: StoryTitle` | `name` | (none; file name) |
| `stories.ifid` (proposed) | `StoryData.ifid` | `ifid` | (none) |
| `passages.title` | passage name | `passages[].name` | knot/stitch name |
| `passages.body` | passage body | `passages[].text` | knot content |
| `passages.is_start` | `StoryData.start` | `startnode` | first line / `-> start` |
| `passages.pos_x/pos_y` | `{"position":"x,y"}` | `position.{x,y}` | (none) |
| `choices.text` / `to_passage` | `[[Text->Target]]` | `links[].{name,link}` | `* [Text] -> target` |
