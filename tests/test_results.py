"""Selection correctness and snapshot lifecycle with synthetic student records."""

import pytest
from pydantic import ValidationError

from uoft_mcp.results import ResultError, ResultStore, Selection, encode, view


def test_nested_filter_page_project_without_mutation():
    rows = [{"code": str(i), "term": "F" if i % 2 else "S", "marks": [i]} for i in range(50)]
    data = {"payload": {"a/b~": rows}}
    before = encode(data)
    selected = view(
        data,
        Selection(
            pointer="/payload/a~1b~0", equals={"term": "F"}, offset=2, limit=3, fields=["code"]
        ),
    )
    assert selected["data"] == [{"code": "5"}, {"code": "7"}, {"code": "9"}]
    assert selected["total"] == 25
    assert selected["next_offset"] == 5
    assert not selected["complete"]
    assert encode(data) == before


@pytest.mark.parametrize("data", [None, True, 3, "hello", {}, []])
def test_small_roots_preserved(data):
    result = view(data, Selection())
    assert result["data"] == data
    assert result["complete"]


def test_heterogeneous_fields_and_missing_values():
    data = [{"a": 1}, {"b": 2}]
    assert view(data, Selection(fields=["a"]))["data"] == [{"a": 1}, {}]
    assert view([], Selection(fields=["unknown"]))["data"] == []
    with pytest.raises(ResultError):
        view(data, Selection(fields=["missing"]))
    with pytest.raises(ResultError):
        view([1, {"a": 2}], Selection(fields=["a"]))
    assert view([{"a": True}, {"a": 1}], Selection(equals={"a": True}))["data"] == [{"a": True}]


@pytest.mark.parametrize(
    "selection",
    [
        {"pointer": "payload"},
        {"pointer": "/~2"},
        {"limit": 101},
        {"offset": -1},
        {"fields": []},
        {"eval": "print(1)"},
    ],
)
def test_invalid_selection_rejected(selection):
    with pytest.raises(ValidationError):
        Selection(**selection)


@pytest.mark.parametrize(
    "data,selection",
    [
        ({}, {"pointer": "/missing"}),
        ([1], {"pointer": "/1"}),
        ([1], {"pointer": "/01"}),
        ({}, {"equals": {"a": 1}}),
        ({}, {"offset": 1}),
        (1, {"fields": ["a"]}),
    ],
)
def test_selection_shape_errors(data, selection):
    with pytest.raises(ResultError):
        view(data, Selection(**selection))


def test_large_object_and_huge_keys_have_bounded_overview():
    data = {"courses": ["é" * 20000], "x" * 20000: "secret"}
    result = view(data, Selection())
    assert not result["complete"] and "data" not in result
    assert len(encode(result).encode()) < 16384
    assert result["overview"]["keys"] == [{"key": "courses", "type": "array"}]
    assert not result["overview"]["keys_complete"]
    assert "secret" not in encode(result)


def test_page_completeness_and_unicode_budget():
    first = view(list(range(25)), Selection())
    assert first["next_offset"] == 20
    last = view(list(range(25)), Selection(offset=20))
    assert last["next_offset"] is None and not last["complete"]
    assert "data" in view("é" * 8191, Selection())
    assert "data" not in view("é" * 8192, Selection())


def test_key_inspection_can_reach_keys_beyond_the_overview():
    data = {f"key{i}": "large" * 1000 for i in range(40)}
    assert view(data, Selection())["overview"]["keys_complete"] is False
    result = view(data, Selection(mode="keys", offset=20, limit=20))
    assert result["data"][0] == {"key": "key20", "type": "string"}
    assert result["next_offset"] is None
    assert result["total"] == 40
    with pytest.raises(ResultError):
        view([], Selection(mode="keys"))


def test_equality_ignores_object_key_order_but_not_array_order_or_types():
    rows = [
        {"nested": {"a": [1, 2], "b": True}},
        {"nested": {"a": [2, 1], "b": True}},
        {"nested": {"a": [1, 2], "b": 1}},
    ]
    result = view(rows, Selection(equals={"nested": {"b": True, "a": [1.0, 2]}}))
    assert result["data"] == rows[:1]


def test_lru_fixed_expiry_and_oversized_entry():
    now = [0]
    store = ResultStore(clock=lambda: now[0], ttl=5, max_entries=2, max_bytes=100)
    a = store.put([1], private=False, generation=0)[0]
    b = store.put([2], private=False, generation=0)[0]
    store.get(a)
    c = store.put([3], private=False, generation=0)[0]
    with pytest.raises(ResultError):
        store.get(b)
    assert store.get(a).data == [1]
    assert store.put("x" * 101, private=False, generation=0)[0] is None
    assert store.get(c).data == [3]
    now[0] = 5
    with pytest.raises(ResultError):
        store.get(a)


def test_memory_budget_and_auth_generation():
    store = ResultStore(max_bytes=12)
    public = store.put([1234], private=False, generation=0)[0]
    private = store.put([2345], private=True, generation=0)[0]
    store.invalidate_private()
    assert store.get(public).data == [1234]
    with pytest.raises(ResultError):
        store.get(private)
    with pytest.raises(ResultError):
        store.put([1], private=True, generation=0)
    store.put([123456], private=False, generation=1)
    with pytest.raises(ResultError):
        store.get(public)
    store.clear()
    assert not store.entries
