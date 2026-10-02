from dataclasses import replace
import json

import pytest

from bdb_shared.evidence import canonical_json_bytes
from bdb_vnext.project_memory import MAX_EXECUTION_BYTES, ProjectMemoryError, ProjectMemoryStore


def test_execution_overflow_rejected_before_replace(tmp_path):
    store = ProjectMemoryStore(tmp_path, "p1")
    store.append_event("PROJECT_CREATED", "Created")
    before = store.memory_path.read_bytes()
    revision = store.read_state().revision
    with pytest.raises(ProjectMemoryError, match="byte bound"):
        store.write_transaction(lambda state: (replace(state, execution={"payload": "x" * MAX_EXECUTION_BYTES}), None))
    assert store.memory_path.read_bytes() == before
    assert store.read_state().revision == revision


@pytest.mark.parametrize("text", ["x", "ą", "😀"])
def test_execution_byte_boundary_round_trip(tmp_path, text):
    store = ProjectMemoryStore(tmp_path, "p1")
    overhead = len(canonical_json_bytes({"payload": ""}))
    count, remainder = divmod(MAX_EXECUTION_BYTES - overhead, len(text.encode("utf-8")))
    execution = {"payload": text * count + "x" * remainder}
    assert len(canonical_json_bytes(execution)) == MAX_EXECUTION_BYTES
    store.write_transaction(lambda state: (replace(state, execution=execution), None))
    assert store.read_state().execution == execution
    before = store.memory_path.read_bytes()
    with pytest.raises(ProjectMemoryError):
        store.write_transaction(lambda state: (replace(state, execution={"payload": execution["payload"] + "x"}), None))
    assert store.memory_path.read_bytes() == before


@pytest.mark.parametrize("patch", [
    {"events": [{}]}, {"events": [None]}, {"events": "bad"}, {"execution": []},
    {"revision": True}, {"revision": "2"}, {"decisions": [{"decision_id": "d1"}]},
    {"execution": {"attempts": ["bad"]}}, {"execution": {"launch_outbox": []}},
])
def test_corrupt_shape_is_typed_and_read_never_changes_bytes(tmp_path, patch):
    store = ProjectMemoryStore(tmp_path, "p1")
    store.append_event("PROJECT_CREATED", "Created")
    document = json.loads(store.memory_path.read_bytes())
    document.update(patch)
    payload = canonical_json_bytes(document)
    store.memory_path.write_bytes(payload)
    with pytest.raises(ProjectMemoryError):
        store.read_state()
    assert store.memory_path.read_bytes() == payload


def test_writer_uses_reader_required_field_contract(tmp_path):
    store = ProjectMemoryStore(tmp_path, "p1")
    event = store.append_event("PROJECT_CREATED", "Created")
    before = store.memory_path.read_bytes()
    with pytest.raises(ProjectMemoryError):
        store.write_transaction(lambda state: (replace(state, events=(replace(event, human_summary=3),)), None))
    assert store.memory_path.read_bytes() == before


def test_invalid_json_is_typed_and_preserved(tmp_path):
    store = ProjectMemoryStore(tmp_path, "p1")
    store.root.mkdir(parents=True)
    store.memory_path.write_bytes(b"{invalid")
    with pytest.raises(ProjectMemoryError) as caught:
        store.read_state()
    assert caught.value.code == "memory_corrupt"
    assert store.memory_path.read_bytes() == b"{invalid"
