"""The MCP tools (mcp_server.py) and the automatic story-so-far (summarize.py), with a stubbed summariser."""

import pytest

from cyoa import db, judge, mcp_server as m, summarize


@pytest.fixture
def calls(tmp_path, monkeypatch):
    """A fresh database and a summariser stub; the list records (base, titles) for each model call."""
    db.DB_PATH = tmp_path / "t.db"
    db.init_db()
    monkeypatch.setattr(summarize, "_down_until", 0.0)
    log: list[tuple[str, list[str]]] = []

    def ask(base, steps):
        log.append((base, [s["title"] for s in steps]))
        return (base + " | " if base else "") + "+".join(s["title"] for s in steps)

    monkeypatch.setattr(summarize, "_ask", ask)
    return log


def book():
    """Gate -> Hall -> Throne and Gate -> Yard -> Throne (two routes to one scene)."""
    sid = m.create_story("Test", "a premise")["id"]
    a = m.add_passage(sid, "Gate", "You reach the gate.")["id"]
    b = m.add_passage(sid, "Hall", "A cold hall.", from_passage=a, choice_text="Go in")["id"]
    c = m.add_passage(sid, "Yard", "A yard.", from_passage=a, choice_text="Circle round")["id"]
    d = m.add_passage(sid, "Throne", "The throne room.", from_passage=b, choice_text="Climb")["id"]
    m.add_choice(sid, c, "Sneak in", to_passage=d)
    return sid, a, b, c, d


def test_arrival_summaries_and_cache(calls):
    sid, a, b, c, d = book()
    got = m.get_passage(sid, d, routes=2)["arrival"]
    assert [r["route"] for r in got["routes"]] == [[{"id": a, "title": "Gate"}, {"id": b, "title": "Hall"}],
                                                   [{"id": a, "title": "Gate"}, {"id": c, "title": "Yard"}]]
    first = got["routes"][0]
    assert first["summary"] == "Gate+Hall" and first["summary_source"] == "model"
    assert "state" not in first                                    # a story has no variables
    n = len(calls)
    m.get_passage(sid, d)                                          # nothing changed: all from cache
    assert len(calls) == n and m.get_passage(sid, d)["arrival"]["routes"][0]["summary_source"] == "cache"


def test_new_passage_extends_cached_prefix_with_one_call(calls):
    sid, a, b, c, d = book()
    m.get_passage(sid, d)
    calls.clear()
    e = m.add_passage(sid, "Crown", "A crown.", from_passage=d, choice_text="Take it")["id"]
    r = m.get_passage(sid, e, routes=1)["arrival"]["routes"][0]
    assert r["summary"] == "Gate+Hall | Throne"
    assert calls == [("Gate+Hall", ["Throne"])]                    # only the new passage was condensed


def test_editing_a_passage_invalidates_its_descendants(calls):
    sid, a, b, c, d = book()
    m.get_passage(sid, d, routes=1)
    m.update_passage(sid, b, body="A warm hall.")
    calls.clear()
    assert m.get_passage(sid, d, routes=1)["arrival"]["routes"][0]["summary_source"] == "model"
    assert calls


def test_model_down_falls_back_to_excerpts_and_backs_off(calls, monkeypatch):
    sid, a, b, c, d = book()
    attempts = []

    def boom(base, steps):
        attempts.append(1)
        raise ConnectionError("llama-server is not up")

    monkeypatch.setattr(summarize, "_ask", boom)
    r = m.get_passage(sid, d, routes=1)["arrival"]["routes"][0]
    assert r["summary_source"] == "excerpt" and "You reach the gate." in r["summary"]
    m.get_passage(sid, d, routes=1)
    assert len(attempts) == 1                                      # cooldown: the dead server was tried once


def test_frontier_wiring_and_problems(calls):
    sid = m.create_story("T")["id"]
    a = m.add_passage(sid, "A", "a")["id"]
    assert m.edit_story(sid)["start_passage_id"] == a              # first passage becomes the start
    dangling = m.add_choice(sid, a, "Onward")["id"]
    assert m.frontier(sid)["dangling_choices"][0]["choice_id"] == dangling
    b = m.add_passage(sid, "B", "b", fill_choice=dangling, is_ending=True)["id"]
    assert m.frontier(sid)["totals"] == {"dangling_choices": 0, "unfinished_passages": 0}
    assert m.problems(sid)["unreachable"] == [] and "undeclared" not in m.problems(sid)
    assert m.delete_passage(sid, b)["now_dangling_choices"] == [dangling]
    with pytest.raises(ValueError):
        m.add_passage(sid, "C", "c", from_passage=a)               # choice_text missing


def test_tool_calls_are_logged_under_the_label(calls):
    sid, *_ = book()
    with pytest.raises(ValueError):
        m.get_passage(sid, 9999)
    with db.transaction() as conn:
        rows = conn.execute("SELECT tool, ok, error FROM tool_calls WHERE story_id = ? ORDER BY id", (sid,)).fetchall()
    assert rows[-1]["tool"] == "get_passage" and not rows[-1]["ok"] and "9999" in rows[-1]["error"]
    assert any(r["tool"] == "add_passage" and r["ok"] for r in rows)


def test_prune_below_and_canon(calls):
    sid, a, b, c, d = book()
    m.lore_save(sid, "Queen", "character", "Cold.")
    assert m.lore_save(sid, "Queen", "character", "Colder.")["updated"]
    assert "Colder." in m.canon(sid)
    gone = m.prune_below(sid, b)
    assert gone["deleted_passages"] == []                           # Throne is still reachable via Yard
    assert m.prune_below(sid, c)["deleted_passages"] == [d]         # now only Yard led there


def test_lore_history_and_impact(calls):
    sid, a, b, c, d = book()
    lid = m.lore_save(sid, "The Hall (keep)", "place", "Cold.")["id"]
    m.lore_save(sid, "The Hall (keep)", "place", "Warm.")
    assert [r["body"] for r in m.lore_history(sid, lid)] == ["Cold."]
    assert [p["title"] for p in m.lore_impact(sid, lid)["passages_mentioning"]] == ["Hall"]
    hid = m.lore_save(sid, "HINGE: gate", "rule", "open or shut")["id"]       # searched by the name after HINGE:
    hits = m.lore_impact(sid, hid)
    assert hits["searched_for"] == ["gate"] and [p["title"] for p in hits["passages_mentioning"]] == ["Gate"]
    m.lore_delete(sid, lid)
    assert m.lore_history(sid, lid)[0]["action"] == "delete"


def test_find_passages_matches_whole_words(calls):
    sid, *_ = book()
    assert [p["title"] for p in m.find_passages(sid, "gate")] == ["Gate"]
    assert m.find_passages(sid, "ate") == []                       # not inside "gate"


def test_endings_report_flags_similar_endings(calls):
    sid, a, b, c, d = book()
    text = "The tide came in over the black rocks and the lamp went out and nobody ever came back to the tower."
    m.add_passage(sid, "Dark", text, from_passage=d, choice_text="Leave it", is_ending=True)
    m.add_passage(sid, "Dim", text + " Gulls.", from_passage=c, choice_text="Run", is_ending=True)
    m.add_passage(sid, "Bright", "Dawn broke gold across the harbour and every bell in the town began to ring.", from_passage=b,
                  choice_text="Wait", is_ending=True)
    rep = m.endings_report(sid)
    assert {e["title"] for e in rep["endings"]} == {"Dark", "Dim", "Bright"}
    assert [(p["a"], p["b"]) for p in rep["similar"]] == [("Dark", "Dim")]
    assert all(e["reachable"] for e in rep["endings"])


def test_delete_story_needs_the_title(calls):
    sid, *_ = book()
    with pytest.raises(ValueError):
        m.delete_story(sid, "nope")
    assert m.delete_story(sid, "Test")["deleted"] == sid


def test_lore_impact_finds_a_character_by_any_word_of_the_name(calls):
    sid, a, b, c, d = book()
    m.update_passage(sid, b, body="Wren waits in the hall.")
    cid = m.lore_save(sid, "Wren Tolliver", "character", "A boatman.")["id"]
    assert [p["title"] for p in m.lore_impact(sid, cid)["passages_mentioning"]] == ["Hall"]


def test_nearest_endings_ranks_similar_endings_and_reports_where_routes_split(calls, monkeypatch):
    from cyoa import embed

    sid = m.create_story("T", "p")["id"]
    root = m.add_passage(sid, "Root", "start")["id"]
    a = m.add_passage(sid, "A", "go a", from_passage=root, choice_text="a")["id"]
    b = m.add_passage(sid, "B", "go b", from_passage=root, choice_text="b")["id"]
    m.add_passage(sid, "A1", "drowned cold sea", from_passage=a, choice_text="a1", is_ending=True)
    m.add_passage(sid, "A2", "drowned cold tide", from_passage=a, choice_text="a2", is_ending=True)
    m.add_passage(sid, "B1", "warm bright feast", from_passage=b, choice_text="b1", is_ending=True)
    vectors = {"drowned cold sea": [1, 0.1, 0], "drowned cold tide": [1, 0.2, 0], "warm bright feast": [0, 1, 1]}
    monkeypatch.setattr(embed, "_fetch", lambda texts: [next(v for k, v in vectors.items() if k in t) for t in texts])
    got = m.nearest_endings(sid, top=1)
    assert got["endings"] == 3
    assert {got["closest"][0]["a"], got["closest"][0]["b"]} == {"A1", "A2"}
    assert got["closest"][0]["diverged_after"] == 2             # Root, A shared
    assert got["per_ending"][2]["nearest"][0]["diverged_after"] == 1
    with pytest.raises(ValueError, match="three endings"):
        m.nearest_endings(m.create_story("E", "p")["id"])


def test_embedding_input_is_short_and_keeps_the_end():
    from cyoa import embed

    text = "start " + "word " * 1000 + "the lamp goes out"
    clipped = embed._clip(text)
    assert len(clipped) <= embed.MAX_CHARS and clipped.startswith("start") and clipped.endswith("the lamp goes out")


def test_a_passage_takes_at_most_three_choices_and_a_refusal_leaves_no_orphan(calls):
    sid = m.create_story("T", "p")["id"]
    root = m.add_passage(sid, "Root", "r")["id"]
    for i in range(m.MAX_CHOICES):
        m.add_passage(sid, f"P{i}", "x", from_passage=root, choice_text=f"c{i}")
    count = lambda: db.connect().execute("SELECT COUNT(*) FROM passages WHERE story_id = ?", (sid,)).fetchone()[0]
    n = count()
    with pytest.raises(ValueError, match="at most 3"):
        m.add_passage(sid, "P3", "x", from_passage=root, choice_text="c3")
    with pytest.raises(ValueError, match="at most 3"):
        m.add_choice(sid, root, "c3")
    assert count() == n                                          # the refused passage was rolled back


def test_fact_check_finds_a_need_one_route_never_revealed(calls):
    """Gate reveals nothing; Hall reveals the key, Yard does not, and both lead to Throne, which needs it."""
    sid, a, b, c, d = book()
    m.lore_save(sid, "key", "fact", "The key is under the mat.")
    with pytest.raises(m.Refused, match="match no lore entry"):
        m.update_passage(sid, b, reveals=["kye"])
    m.update_passage(sid, b, reveals=["KEY"])                       # stored as the fact entry spells it
    m.update_passage(sid, d, needs=["key"], is_ending=True, verdict="solved")
    got = m.fact_check(sid)
    assert got["unmet_needs"] == [{"passage": d, "title": "Throne", "needs": "key", "revealed": "on some routes only",
                                   "route_without_it": ["Gate", "Yard", "Throne"]}]
    assert got["verdicts"] == {"solved": 1} and got["never_revealed"] == []
    assert m.get_passage(sid, d)["facts_on_arrival"] == {"known": [], "on_some_routes_only": ["key"]}
    m.update_passage(sid, c, reveals=["key"])                       # now both routes show it
    assert m.fact_check(sid)["unmet_needs"] == []
    assert m.get_passage(sid, d)["facts_on_arrival"]["known"] == ["key"]
    assert [p["title"] for p in m.lore_impact(sid, 1)["passages_needing"]] == ["Throne"]
    assert m.update_passage(sid, d, verdict="")["meta"] == {"needs": ["key"]}


def test_shape_flags_a_spine_with_exits_in_one_slot(calls):
    """Each step offers 'fail now' first and 'go on' second: a spine with exits, and slot 2 always wins."""
    sid = m.create_story("Spine", "")["id"]
    at = m.add_passage(sid, "Step 0", "Start.")["id"]
    for i in range(1, 5):
        m.add_passage(sid, f"Fail {i}", "You lose.", from_passage=at, choice_text="Rush", is_ending=True, verdict="failed")
        last = i == 4
        at = m.add_passage(sid, f"Step {i}" if not last else "Win", "On.", from_passage=at, choice_text="Wait",
                           is_ending=last, verdict="solved" if last else None)["id"]
    got = m.story_overview(sid)["shape"]
    # the last decision (fail or win) has no trunk: both options end at once
    assert got["decisions"] == 4 and got["instant_exit_decisions"] == 3
    assert got["side_branch_reach"] == {0: 3} and got["trunk_slot"] == {2: 3} and got["exit_slot"] == {1: 4, 2: 1}
    assert got["always_pick"][2] == {"ending": "Win", "verdict": "solved", "decisions": 4}
    assert got["always_pick"][1] == {"ending": "Fail 1", "verdict": "failed", "decisions": 1}
    assert len(got["warnings"]) == 5 and "always picking slot 2 reaches 'Win' (solved" in got["warnings"][-1]

    # a false trail: a third option at the start runs two decisions before it ends, its way on in slot 1
    trail = m.add_passage(sid, "Trail", "A wrong idea that seems to fit.", from_passage=1, choice_text="Follow the tip")["id"]
    deeper = m.add_passage(sid, "Trail turn", "You doubt it.", from_passage=trail, choice_text="Doubt it")["id"]
    m.add_passage(sid, "Trail end", "It falls apart.", from_passage=trail, choice_text="Press on", is_ending=True)
    m.add_passage(sid, "Trail solved", "A different road to the truth.", from_passage=deeper, choice_text="Check", is_ending=True)
    m.add_passage(sid, "Trail lost", "Too late.", from_passage=deeper, choice_text="Wait", is_ending=True)
    got = m.story_overview(sid)["shape"]
    assert got["side_branch_reach"] == {0: 4, 2: 1}
    assert got["exit_slot"] == {1: 5, 2: 3}
    assert got["trunk_slot"] == {1: 1, 2: 3}


def test_fact_check_reports_renamed_facts_and_stray_verdicts(calls):
    sid, a, b, c, d = book()
    f = m.lore_save(sid, "key", "fact", "The key is under the mat.")["id"]
    p = m.add_passage(sid, "Mat", "You lift the mat.", from_passage=d, choice_text="Look", reveals=["key"], verdict="odd")["id"]
    m.lore_save(sid, "brass key", "fact", "The key is under the mat.", lore_id=f)
    got = m.fact_check(sid)
    assert got["unknown_fact_names"] == [{"passage": p, "title": "Mat", "reveals": "key"}]
    assert got["never_revealed"] == ["brass key"]
    assert got["verdicts"] == {"odd": 1} and got["verdict_not_on_an_ending"] == []
    m.update_passage(sid, b, verdict="solved")
    assert m.fact_check(sid)["verdict_not_on_an_ending"] == [{"passage": b, "title": "Hall", "verdict": "solved"}]
    assert m.endings_report(sid)["endings"][0]["verdict"] == "odd"
    assert m.playtest(sid, runs=20, seed=1)["verdicts"] == {"odd": 20}


def test_canon_check_keeps_only_backed_issues_and_compares_fact_use(calls, monkeypatch):
    from cyoa import canon

    sid, a, b, c, d = book()
    m.lore_save(sid, "Throne rule", "rule", "The throne room has no windows.")
    m.lore_save(sid, "key", "fact", "The key is under the mat.")
    m.update_passage(sid, b, reveals=["key"])
    m.update_passage(sid, d, body="The throne room. Light falls through the tall windows. You unlock the door with the key from under the mat.")
    asked = []

    def ask(prompt):
        asked.append(prompt["passage"].split("\n")[0])
        assert "no windows" in prompt["canon"] and "key" not in prompt["canon"]     # facts are given per route, not as canon
        if prompt["passage"].startswith("Throne"):
            return [{"quote": "the tall windows", "evidence": "has no windows", "why": "no windows"},
                    {"quote": "a dragon sleeps", "evidence": "has no windows", "why": "invented quote"},
                    {"quote": "the tall windows", "evidence": "nothing like this", "why": "invented evidence"}]
        return []

    monkeypatch.setattr(canon, "_ask", ask)
    monkeypatch.setattr(canon, "_ask_facts", lambda facts, passage: [{"name": "key", "sentence": "You unlock the door with the key from under the mat."}]
                        if "key" in passage else [{"name": "key", "sentence": "not in the passage"}])
    got = m.canon_check(sid, limit=10)
    assert got["clean"] == 3 and got["remaining"] == [] and got["dropped_unbacked"] == 7   # two bad issues on each of two routes, three unquoted fact uses; a hint is not an issue
    [hit] = got["flagged"]
    assert hit["title"] == "Throne"
    backed, flow = hit["issues"]
    assert backed["quote"] == "the tall windows" and backed["evidence_from"] == "canon" and backed["route"] == ["Gate", "Hall"]
    assert flow["evidence_from"] == "fact flow" and flow["route"] == ["Gate", "Yard"]      # Yard never showed the key
    assert got["hints"] == [{"passage": b, "title": "Hall", "tagged_reveals_not_seen_in_prose": ["key"]}]
    n = len(asked)
    assert m.canon_check(sid, [d])["flagged"][0]["issues"][0]["quote"] == "the tall windows" and len(asked) == n
    assert m.canon_check(sid, [a, b, c, d], limit=1)["remaining"] == []  # all cached: nothing spent


def test_canon_check_refuses_when_the_model_is_down(calls, monkeypatch):
    from cyoa import canon

    sid, a, b, c, d = book()

    def down(*a):
        raise ConnectionError("refused")

    monkeypatch.setattr(canon, "_ask", down)
    with pytest.raises(m.Refused, match="local model unavailable"):
        m.canon_check(sid, [a])


def test_canon_check_reports_an_unreadable_reply_and_carries_on(calls, monkeypatch):
    from cyoa import canon

    sid, a, b, c, d = book()
    tries = []

    def ask(prompt):
        title = prompt["passage"].split("\n")[0]
        tries.append(title)
        if title == "Hall":
            raise canon.json.JSONDecodeError("Expecting ',' delimiter", "{", 1)
        return []

    monkeypatch.setattr(canon, "_ask", ask)
    got = m.canon_check(sid, [a, b, c], limit=10)
    assert got["unchecked"] == [{"passage": b, "title": "Hall", "error": "unreadable model reply: Expecting ',' delimiter: line 1 column 2 (char 1)"}]
    assert got["clean"] == 2 and tries.count("Hall") == 2      # retried once, then reported, not cached


def test_lint_passages_flags_named_defects_with_the_previous_passage_as_context(calls, monkeypatch):
    sid, a, b, c, d = book()
    m.update_passage(sid, d, body="The throne room is cold. The lesson is plain: you should have looked harder.")
    sent = []

    def post(url, json, timeout):
        sent.append(json["state"])
        moral = 0.9 if "lesson is plain" in json["state"].split("PASSAGE TO CHECK:")[-1] else 0.05

        class R:
            def raise_for_status(self): pass
            def json(self): return {"answers": {k: {"type": "noul", "noul": moral if k == "moral" else 0.1} for k in json["questions"]}}
        return R()

    monkeypatch.setattr(judge.httpx, "post", post)
    got = m.lint_passages(sid)
    assert got["checked"] == 4 and [f["title"] for f in got["flagged"]] == ["Throne"]
    assert got["flagged"][0]["defects"] == {"moral": 0.9}
    assert any("PREVIOUS PASSAGE (end):\nA cold hall." in s for s in sent)  # Throne's first parent is Hall
    assert m.lint_passages(sid, [a], threshold=0.01)["flagged"][0]["defects"].keys() == judge.DEFECTS.keys()


def test_lint_passages_refuses_when_the_model_is_down(calls, monkeypatch):
    sid, a, b, c, d = book()

    def down(url, json, timeout):
        raise judge.httpx.ConnectError("refused")

    monkeypatch.setattr(judge.httpx, "post", down)
    with pytest.raises(m.Refused, match="unavailable"):
        m.lint_passages(sid, [a])


def test_quality_map_finds_the_weakest_branch_and_the_book_average(calls, monkeypatch):
    sid = m.create_story("Chase", "a chase")["id"]
    root = m.add_passage(sid, "Start", "Shots ring out and you run.")["id"]
    m.add_passage(sid, "Alley", "You vault a cart. Plums burst underfoot.", from_passage=root, choice_text="Run left")
    dull = m.add_passage(sid, "Street", "dull. It was a street.", from_passage=root, choice_text="Run right")["id"]
    d2 = m.add_passage(sid, "Square", "dull. A square.", from_passage=dull, choice_text="On")["id"]
    d3 = m.add_passage(sid, "Fountain", "dull. A fountain.", from_passage=dull, choice_text="Drink")["id"]
    sent = []

    def post(url, json, timeout):
        sent.append(json["state"])
        p = 0.1 if "PASSAGE TO CHECK:\ndull" in json["state"] else 0.9

        class R:
            def raise_for_status(self): pass
            def json(self): return {"answers": {k: {"type": "noul", "noul": p} for k in json["questions"]}}
        return R()

    monkeypatch.setattr(judge.httpx, "post", post)
    got = m.quality_map(sid, metrics={"vivid": "Is it vivid?"})
    assert got["checked"] == 5 and got["baseline"]["q"] == round((0.9 * 2 + 0.1 * 3) / 5, 3)
    weak = got["weakest_subtrees"]
    assert weak[0]["root"] == dull and weak[0]["size"] == 3 and weak[0]["metrics"] == {"vivid": 0.1}
    assert [p["id"] for p in weak[0]["passages"]] == [dull, d2, d3]
    assert got["lowest_passages"][0]["q"] == 0.1
    assert any("OPTIONS FOR THE READER:\n- Run left\n- Run right" in s for s in sent)  # the choices leaving the start are shown
