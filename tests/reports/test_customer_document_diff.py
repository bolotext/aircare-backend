import pytest
from bson import ObjectId
from common.errors import UnprocessableEntityError

from reports_service.customer_details.service import diff_document, full_document

SCHEMA = [{"key": "age", "type": "int"}, {"key": "name", "type": "string"}]
DOC = {"_id": ObjectId(), "age": 30, "name": "A", "extra": {"k": 1}}


def test_noop():
    assert diff_document(DOC, full_document(DOC), SCHEMA) == ({}, [])


def test_typed_and_free_edits_and_unset():
    edited = {**full_document(DOC), "age": 31, "new": [1]}
    del edited["extra"]
    assert diff_document(DOC, edited, SCHEMA) == ({"age": 31, "new": [1]}, ["extra"])


@pytest.mark.parametrize("edit", [{"age": "x"}, {"_id": "z"}, {"$x": 1}, {"a.b": 1}])
def test_invalid(edit):
    with pytest.raises(UnprocessableEntityError):
        diff_document(DOC, {**full_document(DOC), **edit}, SCHEMA)


def test_cannot_remove_id():
    edited = full_document(DOC)
    del edited["_id"]
    with pytest.raises(UnprocessableEntityError):
        diff_document(DOC, edited, SCHEMA)
