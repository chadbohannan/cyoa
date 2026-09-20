# Web research: the free CYOA / choice-based IF landscape (2026-09-19)

Raw notes from a web research session. Questions asked: what does the free online CYOA community offer — free books, standardized schemas, tools?

## Free / freely readable story corpora

- Project Aon (https://www.projectaon.org) — Joe Dever's Lone Wolf, Freeway Warrior, Combat Heroes gamebooks, published online with the author's permission. Read online or download. Source data historically kept as XML with `<section>` elements and `<choice idref="...">` links; also `mechanics-N.xml` and `objects.xml` rule files consumed by the Kai Chronicles player (github.com/tonib/kaichronicles). Direct XML URLs I tried (`/data/trunk/en/xml/01fftd.xml`, `/en/xml/01fftd.xml`) returned 404; Kai Chronicles uses `npm run downloaddata` via SVN. License: the "Project Aon License" (2019-12-02) — personal use only, no redistribution of "a significant portion", no derivative works incl. translations, no corporate rights. Third-party players (aonic, kaichronicles, gamebook.js) all note the data cannot be redistributed. NOT public domain.
- Fabled Lands (Morris & Thomson) — book text is copyrighted, but the authors have given permission for fan projects (JaFL on SourceForge, github.com/rwgs/web-fabled-lands GPL-2.0 code, book text excluded from the GPL).
- Choice of Games — https://www.choiceofgames.com; several free full-length ChoiceScript games (Choice of the Dragon etc.); most are commercial.
- itch.io — https://itch.io/games/free/made-with-twine/tag-interactive-fiction — the largest live pool of free choice-based IF, mostly Twine (SugarCube/Harlowe HTML), some ChoiceScript, ink, Ren'Py. Licenses vary per game; most are "free to play" not "free to reuse".
- IFDB (https://ifdb.org) — catalog + reviews for all IF, maintained by IFTF. JSON/XML API: `viewgame` (by TUID or IFID; can return iFiction XML) and `search?json`. Full SQL dumps in the IF Archive (ifarchive.org/indexes/if-archive/info/ifdb/). Software is open source (github.com/iftechfoundation/ifdb).
- IF Archive (https://ifarchive.org) — long-running archive of IF games, tools, specs.
- Demian's Gamebook Web Page (https://gamebooks.org) — bibliographic database of printed gamebooks (series, items, people, "Items with Full Text" filter). Documents Chooseco's CYOA series (Series/1425). Documentation only; no texts for CYOA proper.
- Chooseco / cyoa.com — trademark holder of "Choose Your Own Adventure"; books are commercial; Open Library has a CYOA collection for borrowing. No free structured data.
- Public-domain gamebooks essentially do not exist: the genre began in the 1970s–80s, so all originals are still in copyright.

## Standardized formats / schemas

- Twee 3 (IFTF spec, github.com/iftechfoundation/twine-specs/blob/master/twee-3-specification.md). Plain text. Passage header line: `:: Name [tag tag] {"position":"600,400","size":"100,100"}`; body until next `::`. Special passages `StoryTitle` and `StoryData` (JSON: required `ifid` UUID; optional `format`, `format-version`, `start`, `tag-colors`, `zoom`). Special tags `script`, `stylesheet`. `[`, `]`, `{`, `}` in names are backslash-escaped. Links are story-format markup, conventionally `[[Text->Target]]`, `[[Target<-Text]]`, `[[Text|Target]]`, `[[Target]]`. Compilers: Tweego (motoslave.net/tweego), Twine 2 import/export. Twine 2 also has an HTML `<tw-storydata>` / `<tw-passagedata>` format (twine-2-htmloutput-spec).
- Twine JSON exporters: Twison (lazerwalker/twison), TwineJson (cauli/twinejson), twine-to-json (jtschoonhoven). Twison shape: `{name, startnode, creator, creator-version, ifid, passages:[{pid, name, text, tags?, position:{x,y}, links:[{name, link, pid}], props?}]}`.
- ink (inkle, MIT, github.com/inkle/ink). Text language: knots `=== knot ===`, stitches `= stitch`, diverts `->`, choices `*` (once-only) / `+` (sticky), `[bracketed]` choice text, conditional choices `* {cond} text`, `VAR` globals, `~ temp` locals, `{if}` blocks, tunnels `-> x ->`, threads `<- x`, `LIST` enum/set type, `visited` counts. Compiled by `inklecate` to a runtime JSON consumed by ink runtimes (C#, inkjs). Editor: Inky.
- ChoiceScript (dfabulich/choicescript). Text language with `*choice`, `#option`, `*goto`, `*label`, `*set`, `*if`, `*finish`, stat charts. License: ChoiceScript License v1.0 — free non-commercial use, NOT OSI open source (issue #109), commercial requires Choice of Games.
- Treaty of Babel (babel.ifarchive.org) — community standard for IF bibliography: IFIDs (UUID-style unique identifiers) and iFiction XML metadata records; used by IFDB, Inform, Twine's `StoryData.ifid`.
- Project Aon XML — `<section id="sect1">` numbered sections with `<choice idref="sect23">`; separate mechanics/objects XML. De facto schema for Lone Wolf only; not general.
- Other authoring systems listed on IFWiki "Choice-based": Squiffy, Undum, Ren'Py; collaborative CYOA sites MyAdventureGame.com, Protagonize.

## Structural analysis of classic CYOA books

- Christian Swinehart, "One Book, Many Readings" (samizdat.co) — 13-month study of 12 CYOA books; endings classified great/favorable/mediocre/disappointing/catastrophic; found later books were more linear with fewer endings.
- Sean Ragan (Graphviz, Mystery of Chimney Rock), Michael Niggel (Journey Under the Sea: >75% endings unfavorable, 50% death), Greg Lord (Third Planet from Altair interactive map). Collected at samplereality.com/2009/11/11/... and atlasobscura.com/articles/cyoa-choose-your-own-adventure-maps.
- Academic: "Choose Your Own Adventure: An Analysis of Interactive Gamebooks Using Graph Theory" (Journal of Humanistic Mathematics vol 9 iss 2) — 40 books as digraphs; longest path, count of out-degree-zero vertices (endings).
- Neo4j blog: Vampire Express modeled as a graph database.
- MACHIAVELLI benchmark (arXiv) — 134 CYOA (ChoiceScript-derived) games used as an LLM agent benchmark.

## Community hubs

- intfiction.org forum (resource list thread /t/list-of-interactive-fiction-resources/49394), IFWiki (ifwiki.org), IFComp (annual, since 1996), Choice of Games forum, twinery.org cookbook + forum, twinelab.net resources.
