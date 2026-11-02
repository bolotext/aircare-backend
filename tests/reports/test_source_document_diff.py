import pytest
from bson import ObjectId

from reports_service.calls.mapped_fields import WITHHELD, diff_source_document

DOC = {"_id": ObjectId(), "call_id": "c1", "phone": "999", "conversation_log": [{"role": "user"}], "x": 1}


def _shown(**over):
    d = {"_id": str(DOC["_id"]), "call_id": "c1", "phone": WITHHELD, "conversation_log": [{"role": "user"}], "x": 1}
    d.update(over)
    return d


def test_unchanged_document_is_a_noop():
    assert diff_source_document(DOC, _shown()) == ({}, [])


def test_changed_and_removed_keys():
    to_set, to_unset = diff_source_document(DOC, {k: v for k, v in _shown(x=2, y="n").items() if k != "call_id"})
    assert to_set == {"x": 2, "y": "n"} and to_unset == ["call_id"]


def test_conversation_log_is_editable():
    to_set, _ = diff_source_document(DOC, _shown(conversation_log=[]))
    assert to_set == {"conversation_log": []}


@pytest.mark.parametrize("edit", [{"phone": "1"}, {"_id": "z"}, {"$set": 1}, {"a.b": 1}])
def test_forbidden_edits(edit):
    with pytest.raises(ValueError):
        diff_source_document(DOC, _shown(**edit))


def test_cannot_remove_pii_field():
    with pytest.raises(ValueError):
        diff_source_document(DOC, {k: v for k, v in _shown().items() if k != "phone"})
