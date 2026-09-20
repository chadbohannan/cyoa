# The Free CYOA Community: What Exists and What We Can Use

This synthesis answers a question asked on 2026-09-19: what does the free online choose-your-own-adventure community offer — free books, standardized schemas, tools? The raw findings, with URLs, are in [the research notes](../sources/20260919-community-research.md). The short version: there is a mature open ecosystem of *authoring formats* and *catalog standards* but almost no *freely reusable story text*, and no single schema that everyone shares. That shapes what this project should borrow and what it should not expect to find.

## Free books: readable, rarely reusable

The genre is young enough that almost nothing is in the public domain — the one real exception is *Consider the Consequences!* (1930), the first branching book, whose copyright was never renewed; it is scanned on the Internet Archive and has a modern Twine port, as recorded in the [follow-up notes](../sources/20260919-free-text-followup.md). Beyond that, the "Choose Your Own Adventure" name itself is a Chooseco trademark with all titles commercial. What exists instead is a handful of author-permitted online editions and a large pool of free-to-play hobbyist work.

Project Aon is the most complete corpus: Joe Dever's Lone Wolf and related series, transcribed by volunteers as XML with numbered `<section>` elements linked by `<choice idref>` and separate rules files for combat, items and skills. It is the closest thing to a structured dataset of classic gamebooks, but its license (2019) forbids redistributing any significant portion or making derivative works, and the third-party players built on it (Kai Chronicles, aonic, gamebook.js) all fetch the data at install time rather than ship it. The Fabled Lands books are in a similar position: fan ports are GPL, the text is copyrighted but permitted for those ports. Both are useful as *reference designs* for how numbered-section gamebooks encode mechanics — the [story model](../concepts/story-model.md) already anticipates this with its JSON `condition`/`effects` — but they are not import targets we can ship.

The live community publishes on itch.io, where thousands of free Twine, ChoiceScript and ink games exist under per-game terms (free to play, usually not free to reuse). Choice of Games gives away a few full-length ChoiceScript titles. For catalog data rather than text, IFDB exposes a JSON API and full SQL dumps, and Demian's Gamebook Web Page documents the printed genre bibliographically. For genuinely redistributable branching text there are only a few small items: inkle's MIT-licensed demo *The Intercept* (source on GitHub), the 1930 book above, and the ChoiceScript sample game under its non-commercial license. ChooseYourStory.com hosts thousands of free-to-read community storygames (a Hugging Face scrape of it exists, with unclear licensing). So for sample content to exercise the editor, the practical route is to write our own or generate it with the [backend's](../systems/backend.md) Ollama integration; *The Intercept* and *Consider the Consequences!* are the two candidates for an import fixture if we want real prose.

## Standardized schemas: three de-facto standards, one bibliographic treaty

There is no cross-tool CYOA schema. What exists is one text format per major authoring system, plus a shared identifier convention:

- **Twee 3** is the IFTF-maintained plain-text form of Twine stories: `:: PassageName [tags] {json-metadata}` headers, prose bodies, `[[link->Target]]` markup, and a `StoryData` passage carrying a JSON `ifid`, start passage and editor zoom. Twine's own editor is a graph of passages laid out by `position`, which is exactly the shape of our React Flow [frontend](../systems/frontend.md). Community exporters (Twison, TwineJson, twine-to-json) flatten it to JSON with `{passages:[{pid,name,text,tags,position,links:[{name,link,pid}]}]}`.
- **ink** (inkle, MIT) is the richest language for logic: knots and stitches, once-only versus sticky choices, conditional choices, global and temp variables, tunnels, threads and the `LIST` set type, compiled by `inklecate` into a runtime JSON. It is the reference point for what a "condition and effect" vocabulary eventually wants to express.
- **ChoiceScript** is the commercial standard (stat-driven `*choice`/`*set`/`*if`), but its license is non-commercial-only and not OSI open source, so it is a design reference, not something to embed.
- **The Treaty of Babel** defines IFIDs (UUIDs identifying a work) and iFiction XML metadata. Twine, Inform and IFDB all honor it; giving each of our stories an IFID is cheap and makes future export or cataloging interoperable.

How these map onto our tables, and which one an eventual import/export should target, is worked out in [interchange formats](../concepts/interchange-formats.md).

## Structural research on classic books

A small literature analyzes the original CYOA books as directed graphs. Christian Swinehart's *One Book, Many Readings* mapped twelve titles and showed later volumes becoming more linear with fewer endings; individual analyses found that a majority of endings in some books are unfavorable and that up to half kill the reader. An academic study treated forty books as digraphs and measured longest path and the count of out-degree-zero vertices (endings). These are the metrics an authoring tool can compute cheaply from our graph — ending count, ending-quality distribution, longest and shortest read, unreachable passages — and they suggest a "story health" panel as a natural future feature of the editor.

## Where the community talks

The intfiction.org forum, IFWiki, the annual IFComp, the Twine cookbook and forum at twinery.org, and the Choice of Games forum are the active hubs; the IF Archive holds the specs and history. Any future decision to publish an interchange format or ask for feedback should go through those.

## Reference

| Resource | What it offers | Reusable in this project? |
| --- | --- | --- |
| Project Aon | Lone Wolf gamebooks as XML + mechanics files | Read/study only; no redistribution |
| Fabled Lands ports | GPL players, permitted copyrighted text | Read/study only |
| *Consider the Consequences!* (1930) | First branching book; US public domain; scan + Twine port | Yes — import fixture candidate |
| *The Intercept* (inkle) | Complete ink demo, MIT | Yes — import fixture candidate |
| ChooseYourStory.com | Thousands of community storygames | Read only; rights with authors |
| itch.io (Twine/ink/ChoiceScript) | Thousands of free-to-play games | Per-game terms; generally play only |
| Choice of Games | Some free ChoiceScript games | Play only |
| IFDB / IF Archive | Catalog API, SQL dumps, specs | Yes (metadata, specs) |
| Twee 3 spec | Open text format for Twine stories | Yes — import/export candidate |
| Twison JSON | Community Twine→JSON shape | Yes — near our graph shape |
| ink | MIT language + JSON runtime | Yes — logic vocabulary reference |
| ChoiceScript | Non-commercial license | Reference only |
| Treaty of Babel | IFID + iFiction metadata | Yes — adopt IFIDs |
