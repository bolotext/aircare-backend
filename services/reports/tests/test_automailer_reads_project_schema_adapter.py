"""Regression test for a bug where the automailer's emailed report showed
all-zero metrics for a project whose configured report collection is raw
call-center CDR data (report_llm-shaped), while /export.csv for the same
project/window showed the real numbers.

Root cause: run_report (mailer/pipeline.py) resolved only the project's
collection (core.deps.resolve_collection_for_project) and never the schema
adapter that /calls, /dashboard and /export.csv all resolve via
core.deps.resolve_project_source. Without the adapter, compute_metrics ran
its native-Call-schema Mongo query directly against documents that don't
have those field names at all, matching nothing.
"""

from datetime import timedelta

import pytest
from beanie import PydanticObjectId

from reports_service.db.mongodb import get_client
from reports_service.mailer.pipeline import run_report
from reports_service.mailer.window import previous_ist_calendar_day

pytestmark = pytest.mark.asyncio

PROJECT = PydanticObjectId()
ORG = PydanticObjectId()


def _fmt(dt) -> str:
    return dt.strftime("%Y/%m/%d %H:%M:%S +0000")


def _raw_report_llm_doc(window_start, **overrides) -> dict:
    # Timestamps placed an hour into the reporting window so the adapter
    # path's Python-side date filter (calls.repository.matches_filter)
    # actually keeps this call, same as build_mongo_query would for a
    # native-schema document in the same window.
    start = window_start + timedelta(hours=1)
    connected = start + timedelta(seconds=3)
    end = start + timedelta(seconds=32)
    doc = {
        "_id": "raw-1",
        "call_uid": "1739438565473667214",
        "call_status": None,
        "callStatus": "ANSWERED",
        "callStartTime": _fmt(start),
        "callConnectedTime": _fmt(connected),
        "callEndTime": _fmt(end),
        "gnanivb_billing_duration": "28",
        "gnanivb_dial_status": "ANSWER",
        "flow_id": "balic-llm-en",
        "stage": "initial_message",
        "sequence_number": 1,
        "answered_seq": 1,
        "no_answer_seq": 0,
        "redis_data": "empty",
    }
    doc.update(overrides)
    return doc


def _patch_send(monkeypatch, sink: list) -> None:
    async def fake_send_mail(**kwargs):
        sink.append(kwargs)

    monkeypatch.setattr("reports_service.mailer.pipeline.send_mail", fake_send_mail)


async def test_manual_run_resolves_the_projects_schema_adapter_not_just_its_collection(app, monkeypatch):
    window_start, window_end = previous_ist_calendar_day()

    # Same Mongo connection the app itself uses (see db.mongodb.get_client) —
    # resolve_project_source_for_client looks documents up through that
    # shared connection, not a standalone mongomock client. A collection
    # name scoped to this test avoids colliding with
    # calls.schema_adapters.resolve_adapter's module-level detection cache,
    # which is keyed only on (db_name, collection_name).
    collection = get_client()["automailer_schema_adapter_test_db"]["report_llm"]
    await collection.insert_one(_raw_report_llm_doc(window_start))

    async def fake_get_project_config(http_client, project_id):
        from reports_service.core.users_client import ProjectConfig

        return ProjectConfig(
            project_id=project_id,
            organization_id=str(ORG),
            report_db_name="automailer_schema_adapter_test_db",
            report_collection_name="report_llm",
        )

    monkeypatch.setattr("reports_service.core.deps.get_project_config", fake_get_project_config)

    sent = []
    _patch_send(monkeypatch, sent)

    run = await run_report(
        scope=str(PROJECT),
        window_start=window_start,
        window_end=window_end,
        recipients=["a@example.com"],
        triggered_by="manual",
        users_http_client=object(),  # unused: get_project_config is faked above
    )

    assert run.status == "success"
    assert len(sent) == 1
    html = sent[0]["message"]
    assert "<tr><td>Total calls</td><td>1</td></tr>" in html
    assert "<tr><td>Answered calls</td><td>1</td></tr>" in html
