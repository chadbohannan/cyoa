from cyoa import engine

def test_problems():
    passages = [{"id": 1}, {"id": 2}, {"id": 3}, {"id": 4, "is_ending": True}]
    choices = [
        {"id": 10, "from_passage": 1, "to_passage": 2},
        {"id": 11, "from_passage": 2, "to_passage": None},
        {"id": 12, "from_passage": 3, "to_passage": 1},
    ]
    p = engine.problems(1, passages, choices)
    assert p == {
        "no_start": False,
        "unreachable": [3, 4],
        "dangling": [11],
        "endings": [4],
        "unfinished": [],
        "merge_candidates": [],
    }
    assert engine.problems(None, passages, choices)["no_start"] is True
    assert engine.problems(None, passages, choices)["unreachable"] == [1, 2, 3, 4]


def test_endings_vs_unfinished():
    """A decided ending and a stub both have no outgoing choices, but only the
    is_ending one counts as an ending; the other is unfinished, not a problem
    to gloss over. random_playthroughs draws the same line at play time."""
    passages = [{"id": 1, "is_ending": False}, {"id": 2, "is_ending": True}, {"id": 3}]
    choices = [
        {"id": 1, "from_passage": 1, "to_passage": 2},
        {"id": 2, "from_passage": 1, "to_passage": 3},
    ]
    p = engine.problems(1, passages, choices)
    assert p["endings"] == [2] and p["unfinished"] == [3]

    r = engine.random_playthroughs(1, passages, choices, runs=50, seed=1)
    assert set(r["endings"]) <= {2} and set(r["unfinished"]) <= {3}
    assert r["endings"].get(2, 0) + r["unfinished"].get(3, 0) == 50


def test_subtree_stops_at_shared_passages():
    passages = [{"id": i} for i in range(1, 7)]
    link = lambda i, a, b: {"id": i, "from_passage": a, "to_passage": b}
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
    passages = [{"id": 1}, {"id": 2}, {"id": 3, "is_ending": True}, {"id": 4}]
    link = lambda i, a, b: {"id": i, "from_passage": a, "to_passage": b}
    # 1 -> 2 -> 3 (ending) | 2 -> 1 (a loop back) | 2 -> nowhere ; 4 is an orphan
    choices = [link(10, 1, 2), link(11, 2, 3), link(12, 2, 1), link(13, 2, None)]
    r = engine.random_playthroughs(1, passages, choices, runs=200, seed=0)
    assert r["unvisited"] == [4] and r["untaken"] == []
    assert set(r["endings"]) == {3} and set(r["dangling"]) == {13} and r["unfinished"] == {}
    assert r["endings"][3] + r["dangling"][13] <= 200 and r["cut"] == 0
    assert "stuck" not in r
    r = engine.random_playthroughs(1, passages, choices, runs=3, max_steps=1, seed=0)
    assert r["cut"] == 3 and r["mean_length"] == 1.0
    assert engine.random_playthroughs(None, passages, choices, runs=3)["visited"] == {}
    assert engine.random_playthroughs(1, passages, choices, runs=2, seed=1) == engine.random_playthroughs(1, passages, choices, runs=2, seed=1)
