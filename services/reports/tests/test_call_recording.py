"""Exercises GET /calls/{conversation_id}/recording (calls/recording.py):
resolving flow_id/call_date/call_uid from a raw adapter-sourced document,
proxying the internal audio server, and the cases where no recording is
resolvable at all (native-Call-schema project, unknown conversation_id, or
a source document missing flow_id/call_uid).
"""

import pytest
from beanie import PydanticObjectId
from mongomock_motor import AsyncMongoMockClient

from reports_service.core.deps import ProjectCallSource, get_current_identity, resolve_project_source
from reports_service.calls.schema_adapters import normalize_call_logs
from tests.factories import make_call, make_identity

pytestmark = pytest.mark.asyncio

PROJECT = PydanticObjectId()
ORG = PydanticObjectId()


def _raw_call_logs_doc(**overrides) -> dict:
    doc = {
        "_id": "raw-1",
        "conversation_id": "1790329075540158485",
        "call_uid": "1790329075540158485",
        "bot_id": "23d97519803144b199bb1986b67e7c78",
        "flow_id": "prod-b6117f2226cd",
        "language": "english",
        "call_status": "ANSWERED",
        "DISPOSITION": "APPOINTMENT_INTERESTED",
        "callStartTime": "ISODate(2026-09-25T09:37:55+00:00)",
        "callConnectedTime": "ISODate(2026-09-25T09:37:59+00:00)",
        "callEndTime": "ISODate(2026-09-25T09:38:53+00:00)",
        "tempDate": "25/09/2026",
        "gnanivb_billing_duration": "54",
        "gnanivb_dial_status": "ANSWER",
        "rec_path": "prod-b6117f2226cd/2026_09_25/prod-b6117f2226cd-1790329075540158485-call.wav",
    }
    doc.update(overrides)
    return doc


def _override_call_logs_source(app, collection) -> None:
    app.dependency_overrides[resolve_project_source] = lambda: ProjectCallSource(
        collection=collection, organization_id=str(ORG), adapter=normalize_call_logs
    )


async def test_recording_proxies_the_audio_server_with_the_right_lookup_fields(app, client, monkeypatch):
    collection = AsyncMongoMockClient()["aircare"]["call_logs"]
    await collection.insert_one(_raw_call_logs_doc())
    _override_call_logs_source(app, collection)
    app.dependency_overrides[get_current_identity] = lambda: make_identity("ADMIN")

    captured = {}

    async def fake_fetch(ref):
        captured["ref"] = ref
        return b"RIFF....WAVEfmt ", "audio/wav"

    monkeypatch.setattr("reports_service.calls.router.fetch_recording_audio", fake_fetch)

    resp = await client.get(
        "/api/v1/calls/1790329075540158485/recording", params={"project_id": str(PROJECT)}
    )

    assert resp.status_code == 200
    assert resp.headers["content-type"] == "audio/wav"
    assert resp.content == b"RIFF....WAVEfmt "

    ref = captured["ref"]
    assert ref.flow_id == "prod-b6117f2226cd"
    assert ref.call_uid == "1790329075540158485"
    assert ref.call_date == "2026_09_25"


async def test_recording_404s_for_native_call_schema_project(app, client, monkeypatch):
    # The default `app` fixture override (see conftest.py) is a native
    # Call-schema project (adapter=None) — no known recording source.
    await make_call(PROJECT, ORG, conversation_id="native-1")
    app.dependency_overrides[get_current_identity] = lambda: make_identity("ADMIN")

    called = False

    async def fake_fetch(ref):
        nonlocal called
        called = True
        return b"", "audio/wav"

    monkeypatch.setattr("reports_service.calls.router.fetch_recording_audio", fake_fetch)

    resp = await client.get("/api/v1/calls/native-1/recording", params={"project_id": str(PROJECT)})

    # The service's error envelope always ships over HTTP 200 (see
    # common.errors.register_exception_handlers) — the real status lives in
    # the body's `code` field, same convention every other test here follows.
    assert resp.json()["code"] == 404
    assert called is False


async def test_recording_404s_for_unknown_conversation_id(app, client):
    collection = AsyncMongoMockClient()["aircare"]["call_logs"]
    await collection.insert_one(_raw_call_logs_doc())
    _override_call_logs_source(app, collection)
    app.dependency_overrides[get_current_identity] = lambda: make_identity("ADMIN")

    resp = await client.get("/api/v1/calls/does-not-exist/recording", params={"project_id": str(PROJECT)})

    assert resp.json()["code"] == 404


async def test_recording_404s_when_source_document_has_no_flow_id(app, client):
    collection = AsyncMongoMockClient()["aircare"]["call_logs"]
    await collection.insert_one(_raw_call_logs_doc(flow_id=None))
    _override_call_logs_source(app, collection)
    app.dependency_overrides[get_current_identity] = lambda: make_identity("ADMIN")

    resp = await client.get(
        "/api/v1/calls/1790329075540158485/recording", params={"project_id": str(PROJECT)}
    )

    assert resp.json()["code"] == 404


async def test_recording_502s_when_audio_server_fails(app, client, monkeypatch):
    collection = AsyncMongoMockClient()["aircare"]["call_logs"]
    await collection.insert_one(_raw_call_logs_doc())
    _override_call_logs_source(app, collection)
    app.dependency_overrides[get_current_identity] = lambda: make_identity("ADMIN")

    async def failing_fetch(ref):
        from common.errors import BadGatewayError

        raise BadGatewayError("Unable to reach the recording server")

    monkeypatch.setattr("reports_service.calls.router.fetch_recording_audio", failing_fetch)

    resp = await client.get(
        "/api/v1/calls/1790329075540158485/recording", params={"project_id": str(PROJECT)}
    )

    assert resp.json()["code"] == 502
