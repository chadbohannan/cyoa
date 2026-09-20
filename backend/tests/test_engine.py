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

    passages = [{"id": 1}, {"id": 2}, {"id": 3}, {"id": 4}]
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
    }
    assert engine.problems(None, passages, choices, [])["no_start"] is True
    assert engine.problems(None, passages, choices, [])["unreachable"] == [1, 2, 3, 4]
