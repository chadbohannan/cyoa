from cyoa import engine

STATE = {"brave": False, "gold": 10, "lamp": 0}


def test_check_clauses_and_combinators():
    assert engine.check({}, STATE)
    assert engine.check({"var": "gold", "op": ">=", "value": 10}, STATE)
    assert not engine.check({"var": "brave", "op": "==", "value": True}, STATE)
    assert engine.check({"any": [{"var": "brave", "op": "==", "value": True}, {"var": "gold", "op": ">", "value": 5}]}, STATE)
    assert not engine.check({"all": [{"var": "brave", "op": "==", "value": True}, {"var": "gold", "op": ">", "value": 5}]}, STATE)
    # undeclared variable, unknown op, malformed shape, uncomparable types: all false
    assert not engine.check({"var": "sword", "op": "==", "value": True}, STATE)
    assert not engine.check({"var": "gold", "op": "~", "value": 1}, STATE)
    assert not engine.check("nope", STATE)
    assert not engine.check({"var": "gold", "op": "<", "value": "many"}, STATE)


def test_apply_effects():
    out = engine.apply({"set": {"brave": True}, "add": {"gold": -5}, "give": ["lamp", "lamp"], "take": ["gold"]}, STATE)
    assert out == {"brave": True, "gold": 4, "lamp": 2}
    assert STATE["gold"] == 10  # input untouched
    # take floors at zero, undeclared names are ignored, non-numeric add is ignored
    out = engine.apply({"take": ["lamp"], "set": {"sword": 1}, "add": {"brave": 1}}, STATE)
    assert out == STATE


def test_vars_and_problems():
    cond = {"all": [{"var": "a", "op": "==", "value": 1}, {"any": [{"var": "b", "op": "==", "value": 1}]}]}
    assert engine.condition_vars(cond) == {"a", "b"}
    assert engine.effect_vars({"set": {"x": 1}, "give": ["y"], "take": ["z"], "add": {"w": 1}}) == {"w", "x", "y", "z"}

    passages = [{"id": 1}, {"id": 2}, {"id": 3}, {"id": 4, "is_ending": True}]
    choices = [
        {"id": 10, "from_passage": 1, "to_passage": 2, "condition": {}, "effects": {"set": {"seen": True}}},
        {"id": 11, "from_passage": 2, "to_passage": None, "condition": {"var": "ghost", "op": "==", "value": 1}, "effects": {}},
        {"id": 12, "from_passage": 3, "to_passage": 1, "condition": {}, "effects": {}},
    ]
    p = engine.problems(1, passages, choices, [{"name": "seen"}])
    assert p == {
        "no_start": False,
        "unreachable": [3, 4],
        "dangling": [11],
        "undeclared": [{"choice_id": 11, "var": "ghost"}],
        "endings": [4],
        "unfinished": [],
        "merge_candidates": [],
    }
    assert engine.problems(None, passages, choices, [])["no_start"] is True
    assert engine.problems(None, passages, choices, [])["unreachable"] == [1, 2, 3, 4]


def test_endings_vs_unfinished():
    """A decided ending and a stub both have no outgoing choices, but only the
    is_ending one counts as an ending; the other is unfinished, not a problem
    to gloss over. random_playthroughs draws the same line at play time."""
    passages = [{"id": 1, "is_ending": False}, {"id": 2, "is_ending": True}, {"id": 3}]
    choices = [
        {"id": 1, "from_passage": 1, "to_passage": 2, "condition": {}, "effects": {}},
        {"id": 2, "from_passage": 1, "to_passage": 3, "condition": {}, "effects": {}},
    ]
    p = engine.problems(1, passages, choices, [])
    assert p["endings"] == [2] and p["unfinished"] == [3]

    r = engine.random_playthroughs(1, passages, choices, [], runs=50, seed=1)
    assert set(r["endings"]) <= {2} and set(r["unfinished"]) <= {3}
    assert r["endings"].get(2, 0) + r["unfinished"].get(3, 0) == 50


def test_subtree_stops_at_shared_passages():
    passages = [{"id": i} for i in range(1, 7)]
    link = lambda i, a, b: {"id": i, "from_passage": a, "to_passage": b, "condition": {}, "effects": {}}
    # 1 -> 2 -> 3 -> 5 ; 1 -> 4 -> 5 ; 6 is an orphan root linking to 3
    choices = [link(10, 1, 2), link(11, 2, 3), link(12, 3, 5), link(13, 1, 4), link(14, 4, 5), link(15, 6, 3), link(16, 2, None)]
    assert engine.subtree(2, 1, passages, choices) == [2]  # 3 is reachable from the orphan 6, 5 via 4
    assert engine.subtree(4, 1, passages, choices) == [4]
    assert engine.subtree(1, 1, passages, choices) == [1, 2, 4]
    assert engine.subtree(6, 1, passages, choices) == [6]
    assert engine.subtree(99, 1, passages, choices) == []
    # without the orphan, 2's branch owns 3 and shares 5
    assert engine.subtree(2, 1, passages[:5], choices[:5]) == [2, 3]


def test_random_playthroughs_coverage():
    passages = [{"id": i} for i in range(1, 5)]
    choices = [
        {"id": 10, "from_passage": 1, "to_passage": 2, "condition": {}, "effects": {"add": {"n": 1}}},
        {"id": 11, "from_passage": 2, "to_passage": 1, "condition": {"var": "n", "op": "<", "value": 3}, "effects": {}},
        {"id": 12, "from_passage": 2, "to_passage": 3, "condition": {"var": "n", "op": ">=", "value": 3}, "effects": {}},
        {"id": 13, "from_passage": 3, "to_passage": 4, "condition": {"var": "n", "op": ">", "value": 99}, "effects": {}},
    ]
    r = engine.random_playthroughs(1, passages, choices, [{"name": "n", "default": 0}], runs=5, seed=0)
    # every walk loops until n == 3, reaches 3 and is stuck there (a gated choice nobody can meet)
    assert r["stuck"] == {3: 5} and r["endings"] == {} and r["unvisited"] == [4] and r["untaken"] == [13]
    assert r["visited"] == {1: 15, 2: 15, 3: 5} and r["mean_length"] == 6.0 and r["cut"] == 0
    r = engine.random_playthroughs(1, passages, choices, [{"name": "n", "default": 0}], runs=3, max_steps=2, seed=0)
    assert r["cut"] == 3 and r["mean_length"] == 2.0
    assert engine.random_playthroughs(None, passages, choices, [], runs=3)["visited"] == {}
    assert engine.random_playthroughs(1, passages, choices, [], runs=2, seed=1) == engine.random_playthroughs(1, passages, choices, [], runs=2, seed=1)
