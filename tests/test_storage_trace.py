from brains.storage.migrations import init_db
from brains.storage.repositories import list_traces, write_trace


def test_trace_write():
    init_db()
    write_trace("test", "{}")
    traces = list_traces(limit=1)
    assert traces
    assert traces[0].route == "test"
