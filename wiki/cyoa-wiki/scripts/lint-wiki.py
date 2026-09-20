#!/usr/bin/env python3
"""Lint the wiki: broken internal links, orphan pages, and reference counts.

Implements the structural checks from the lint operation in CLAUDE.md:
  1. Link integrity: every relative .md link resolves to an existing file.
  2. Orphan pages: pages not referenced from any non-index page.
  3. Reference counts: how many pages link to each page (visibility signal).

Does not check: contradictions between pages, mentioned-but-missing entities,
or staleness — those require reading prose and live elsewhere in the workflow.

Usage:
  scripts/lint-wiki.py [wiki-dir]

Default wiki-dir is ./wiki relative to the script's parent.
"""

import os
import re
import sys
import urllib.parse
from collections import defaultdict


LINK_RE = re.compile(r"\]\(([^)#]+\.md)(?:#[^)]*)?\)")
SKIP_FILES = {"index.md", "log.md", "CLAUDE.md"}


def load_pages(root: str) -> dict[str, str]:
    pages: dict[str, str] = {}
    for dirpath, _, files in os.walk(root):
        if "/.git" in dirpath:
            continue
        for f in files:
            if f.endswith(".md"):
                p = os.path.normpath(os.path.join(dirpath, f))
                with open(p) as fh:
                    pages[p] = fh.read()
    return pages


def check_links(
    pages: dict[str, str], sources_root: str | None
) -> list[tuple[str, str, str]]:
    broken: list[tuple[str, str, str]] = []
    for path, content in pages.items():
        base = os.path.dirname(path)
        for m in LINK_RE.finditer(content):
            target = urllib.parse.unquote(m.group(1))
            if target.startswith(("http://", "https://")):
                continue
            resolved = os.path.normpath(os.path.join(base, target))
            if resolved in pages:
                continue
            # Links escaping the wiki dir (e.g. ../sources/foo.md) — verify
            # against the on-disk sources directory if provided.
            if sources_root and resolved.startswith("../"):
                external = os.path.normpath(os.path.join(sources_root, resolved))
                if os.path.isfile(external):
                    continue
            broken.append((path, target, resolved))
    return broken


def find_orphans(pages: dict[str, str]) -> list[str]:
    """Pages not referenced from any non-index page."""
    referenced: set[str] = set()
    for path, content in pages.items():
        if os.path.basename(path) == "index.md":
            continue
        base = os.path.dirname(path)
        for m in LINK_RE.finditer(content):
            target = m.group(1)
            if target.startswith(("http://", "https://")):
                continue
            referenced.add(os.path.normpath(os.path.join(base, target)))

    orphans: list[str] = []
    for path in pages:
        if os.path.basename(path) in SKIP_FILES:
            continue
        if path not in referenced:
            orphans.append(path)
    return sorted(orphans)


def reference_counts(pages: dict[str, str]) -> dict[str, int]:
    """Count distinct pages that link to each target page."""
    counts: dict[str, int] = defaultdict(int)
    for path, content in pages.items():
        base = os.path.dirname(path)
        seen_in_this_page: set[str] = set()
        for m in LINK_RE.finditer(content):
            target = m.group(1)
            if target.startswith(("http://", "https://")):
                continue
            resolved = os.path.normpath(os.path.join(base, target))
            seen_in_this_page.add(resolved)
        for r in seen_in_this_page:
            counts[r] += 1
    return counts


def main() -> int:
    wiki_dir = sys.argv[1] if len(sys.argv) > 1 else "wiki"
    if not os.path.isdir(wiki_dir):
        print(f"error: {wiki_dir} is not a directory", file=sys.stderr)
        return 2

    sources_root = os.path.abspath(wiki_dir)
    cwd = os.getcwd()
    os.chdir(wiki_dir)
    try:
        pages = load_pages(".")
    finally:
        os.chdir(cwd)

    print(f"Loaded {len(pages)} markdown pages from {wiki_dir}/")

    broken = check_links(pages, sources_root)
    print("\n=== Broken links ===")
    if broken:
        for src, tgt, res in broken:
            print(f"  {src} -> {tgt}  (resolved: {res})")
    else:
        print("  none")

    orphans = find_orphans(pages)
    print("\n=== Orphan pages (not linked from any non-index page) ===")
    if orphans:
        for o in orphans:
            print(f"  {o}")
    else:
        print("  none")

    counts = reference_counts(pages)
    low_ref = [
        p for p in pages
        if os.path.basename(p) not in SKIP_FILES
        and counts.get(p, 0) <= 1
        and p not in orphans
    ]
    print("\n=== Low-reference pages (linked from exactly 1 page, often only the index) ===")
    if low_ref:
        for p in sorted(low_ref):
            print(f"  {p}  ({counts[p]} link)")
    else:
        print("  none")

    return 1 if broken else 0


if __name__ == "__main__":
    sys.exit(main())
