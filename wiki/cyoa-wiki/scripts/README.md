# Wiki Helper Scripts

Tooling that supports the operations defined in [`../CLAUDE.md`](../CLAUDE.md). Scripts are deliberately stdlib-only (Python 3, no third-party packages) so they run anywhere.

| Script | What it does | Operation |
|--------|--------------|-----------|
| `lint-wiki.py` | Structural lint: broken internal links, orphan pages, and reference counts. Does **not** check contradictions or staleness — those need prose reading. Exit code 1 if broken links are found. Run as `scripts/lint-wiki.py .` from the wiki folder. | Lint |

```bash
# From wiki/cyoa-wiki/
scripts/lint-wiki.py .
```
