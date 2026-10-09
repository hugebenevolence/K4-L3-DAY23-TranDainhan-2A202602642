"""Deterministic merge rule for a small active-active metadata example.

Writers advance a Lamport clock after observing remote writes. A concurrent
tie uses region ID. Losing events stay in the conflict list for audit/replay.
"""


def merge(events: list[dict]) -> tuple[dict, list[dict]]:
    if not events:
        raise ValueError("at least one event is required")
    if len({event["event_id"] for event in events}) != len(events):
        raise ValueError("duplicate event_id")
    ordered = sorted(events, key=lambda e: (e["clock"], e["region"], e["event_id"]))
    winner = ordered[-1]
    return winner, ordered[:-1]


if __name__ == "__main__":
    writes = [
        {"event_id": "a-7", "clock": 7, "region": "a", "body": "A update"},
        {"event_id": "b-7", "clock": 7, "region": "b", "body": "B update"},
    ]
    print(merge(writes))
