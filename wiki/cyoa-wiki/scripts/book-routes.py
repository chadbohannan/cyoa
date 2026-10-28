"""Print every start-to-ending route of an exported Twee story, with word counts.

usage: python3 routes.py <export_twee tool-result .txt> [--text TITLE ...]
"""
import json, re, sys

raw = json.load(open(sys.argv[1]))["result"]
passages, links = {}, {}
for block in raw.split("\n:: ")[1:]:
    head, _, body = block.partition("\n")
    title = re.sub(r"\s*\[.*$", "", head).strip()
    if title in ("StoryTitle", "StoryData"):
        continue
    targets = re.findall(r"\[\[.*?->(.*?)\]\]", body)
    prose = re.sub(r"\[\[.*?\]\]", "", body).strip()
    passages[title], links[title] = prose, targets

wc = lambda t: len(passages[t].split())
start = "Gust at One O'Clock"

def walk(t, path):
    path = path + [t]
    if not links[t]:
        yield path
    for n in links[t]:
        yield from walk(n, path)

if "--text" in sys.argv:
    for t in sys.argv[sys.argv.index("--text") + 1:]:
        print(f"== {t} ({wc(t)}w)\n{passages[t]}\n")
    sys.exit()

for route in walk(start, []):
    end = route[-1]
    print(f"{wc(end):4}w end | " + " > ".join(route[1:]))
words = {t: wc(t) for t in passages}
print("\nendings:", sorted(wc(r[-1]) for r in walk(start, [])))
print("total words:", sum(words.values()))
for phrase in ["garage", "both arms", "both hands", "thirty years", "leaning toward", "lean toward", "He doesn't need to", "She doesn't need to"]:
    hits = [t for t, p in passages.items() if phrase.lower() in p.lower()]
    print(f"{phrase!r}: {hits}")
