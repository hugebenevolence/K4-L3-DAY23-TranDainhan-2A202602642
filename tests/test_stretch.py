"""Checks for the optional active-active routing and merge rule."""
import importlib

from stretch.conflicts import merge


def test_active_active_round_robin(monkeypatch):
    monkeypatch.setenv("EDGE_MODE", "active_active")
    from edge import proxy
    importlib.reload(proxy)
    try:
        assert [proxy.choose_region() for _ in range(10)] == list("ababababab")
    finally:
        monkeypatch.setenv("EDGE_MODE", "active_passive")
        importlib.reload(proxy)


def test_concurrent_write_tie_is_deterministic_and_audited():
    a = {"event_id": "a-7", "clock": 7, "region": "a", "body": "A update"}
    b = {"event_id": "b-7", "clock": 7, "region": "b", "body": "B update"}
    assert merge([a, b]) == merge([b, a]) == (b, [a])
