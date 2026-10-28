"""Spike: per-passage defect checklist (noul) and pairwise comparison (choice) with clef-flash-8k.

Planted defects on current passages (a moral line, a clock closer, scrambled sentences) against untouched originals;
plus the real Sheriff before/after pair. Prints detection rates, not per-call noise.
"""
import json, random, re, sqlite3, statistics as st, sys, time
import httpx

MODEL = "clef-flash-8k"
URL = "http://localhost:11434/v1/systemone"
db = sqlite3.connect("file:/home/chad/workspace/games/cyoa/backend/cyoa.db?mode=ro", uri=True)


def ask(state, questions):
    r = httpx.post(URL, json={"model": MODEL, "state": state, "questions": questions}, timeout=90)
    r.raise_for_status()
    return r.json()["answers"]


DEFECTS = {
    "moral": "Does the passage end with a sentence that spells out a lesson or moral for the reader?",
    "clock": "Does the passage end by stating a clock time?",
    "summary": "Is the passage mostly a summary of events told from a distance, rather than a scene in a concrete moment?",
    "hook": "Does the passage end on a striking image, question or hook that makes the reader want to go on?",
}
Q = {k: {"type": "noul", "instructions": v} for k, v in DEFECTS.items()}


def check(prev, body):
    state = (f"PREVIOUS PASSAGE:\n{prev}\n\n" if prev else "") + f"PASSAGE TO CHECK:\n{body}"
    a = ask(state, Q)
    return {k: a[k]["noul"] for k in Q}


def pair(prev, a, b):
    """P(first is better) averaged over both orders, to cancel position bias."""
    qs = {"better": {"type": "choice", "instructions": "Which version of the passage is better written: concrete, vivid, shown as a scene, and addressed to the reader?",
                     "criteria": {"A": "version A", "B": "version B"}}}
    out = []
    for first, second, mine in ((a, b, "A"), (b, a, "B")):
        state = (f"PREVIOUS PASSAGE:\n{prev}\n\n" if prev else "") + f"VERSION A:\n{first}\n\nVERSION B:\n{second}"
        r = ask(state, qs)["better"]
        pa = r["probabilities"]["A"]
        out.append(pa if mine == "A" else 1 - pa)
    return st.mean(out), out


def sentences(t):
    return [s for s in re.split(r"(?<=[.!?])\s+", t.strip()) if s]


rows = db.execute("""select p.id, p.story_id, p.title, p.body, p.is_ending,
   (select pp.body from choices c join passages pp on pp.id=c.from_passage where c.to_passage=p.id order by c.id limit 1) as prev
   from passages p order by p.story_id, p.id""").fetchall()
rows = [r for r in rows if 400 < len(r[3]) < 3500]
random.seed(7)
sample = random.sample(rows, min(60, len(rows)))
print(len(rows), "passages eligible,", len(sample), "sampled")

MORAL = "The lesson is plain: you should have looked harder before you spoke."
CLOCK = "It is a quarter to two."
t0 = time.time()
res = {"orig": [], "moral": [], "clock": []}
for pid, sid, title, body, end, prev in sample:
    res["orig"].append(check(prev, body))
    res["moral"].append(check(prev, body.rstrip() + " " + MORAL))
    res["clock"].append(check(prev, body.rstrip() + " " + CLOCK))
print("checklist time", round(time.time() - t0), "s")


def rate(vs, key, thr=0.5):
    return sum(v[key] > thr for v in vs) / len(vs)


print("\nP(question yes) -> fraction above 0.5 / mean")
for k in DEFECTS:
    print(f"{k:8} orig {rate(res['orig'],k):.2f}/{st.mean(v[k] for v in res['orig']):.2f}   +moral {rate(res['moral'],k):.2f}/{st.mean(v[k] for v in res['moral']):.2f}   +clock {rate(res['clock'],k):.2f}/{st.mean(v[k] for v in res['clock']):.2f}")

# pairwise: original vs scrambled sentences, vs moral appended
t0 = time.time()
wins = {"scrambled": [], "moral": []}
for pid, sid, title, body, end, prev in sample[:30]:
    s = sentences(body)
    if len(s) < 4:
        continue
    sh = s[:]
    random.Random(pid).shuffle(sh)
    wins["scrambled"].append(pair(prev, body, " ".join(sh))[0])
    wins["moral"].append(pair(prev, body, body.rstrip() + " " + MORAL)[0])
print("\npairwise time", round(time.time() - t0), "s")
for k, v in wins.items():
    print(f"original vs {k}: mean P(original better) {st.mean(v):.2f}, prefers original {sum(x>0.5 for x in v)}/{len(v)}")

# the real pair
OLD = """Sheriff Dunn arrives in four minutes with two deputies and a great deal of boot. He dusts the safe for fingerprints, and the gray powder settles over the white chalk on the dial and takes it away forever. He searches the dressing rooms, the shop and the cast's handbags, and declares it an inside job in a voice that carries to the lobby. By evening the Playhouse is sealed with yellow tape until Monday. Saturday's audience gets refunds. Somewhere in the village a woman buys a ferry ticket for a trunk that nobody has been told to open."""
new = db.execute("select body from passages where story_id=3 and title='The Sheriff Arrives'").fetchone()[0]
prev = db.execute("select body from passages where story_id=3 and title='The Dial'").fetchone()[0]
print("\nSheriff old:", {k: round(v, 2) for k, v in check(prev, OLD).items()})
print("Sheriff new:", {k: round(v, 2) for k, v in check(prev, new).items()})
print("P(new better than old), both orders:", [round(x, 2) for x in pair(prev, new, OLD)[1]])
