# Wiki Helper Scripts

Tooling that supports the operations defined in [`../CLAUDE.md`](../CLAUDE.md). Scripts are deliberately stdlib-only (Python 3, no third-party packages) so they run anywhere.

| Script | What it does | Operation |
|--------|--------------|-----------|
| `lint-wiki.py` | Structural lint: broken internal links, orphan pages, and reference counts. Does **not** check contradictions or staleness — those need prose reading. Exit code 1 if broken links are found. Run as `scripts/lint-wiki.py .` from the wiki folder. | Lint |
| `book-routes.py` | Reads a saved `export_twee` result (the tool-result `.txt`, JSON with a `result` field) and prints every start-to-ending route with the ending's word count, the book's word total, and which passages repeat stock phrases; `--text TITLE ...` prints passages. Used for the final read-through in [Writing a CYOA Book](../runbooks/writing-a-cyoa-book.md). | Authoring |

```bash
# From wiki/cyoa-wiki/
scripts/lint-wiki.py .
```
