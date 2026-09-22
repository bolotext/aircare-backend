import pytest
from beanie import PydanticObjectId

from reports_service.mailer import smtp_client
from reports_service.mailer.pipeline import run_report
from reports_service.mailer.window import previous_ist_calendar_day
from reports_service.reports.model import ReportRun

pytestmark = pytest.mark.asyncio

PROJECT = PydanticObjectId()


def _patch_send(monkeypatch, sink: list) -> None:
    async def fake_send_once(message):
        sink.append(message)

    monkeypatch.setattr(smtp_client, "_smtp_send_once", fake_send_once)


async def test_second_scheduled_run_same_scope_and_day_is_skipped_not_resent(app, monkeypatch):
    sent = []
    _patch_send(monkeypatch, sent)
    window_start, window_end = previous_ist_calendar_day()

    first = await run_report(
        scope=str(PROJECT), window_start=window_start, window_end=window_end, recipients=["a@example.com"], triggered_by="scheduled"
    )
    assert first.status == "success"

    second = await run_report(
        scope=str(PROJECT), window_start=window_start, window_end=window_end, recipients=["a@example.com"], triggered_by="scheduled"
    )
    assert second.status == "skipped_duplicate"

    assert len(sent) == 1  # the duplicate attempt never actually sent anything
    persisted = await ReportRun.find(ReportRun.scope == str(PROJECT)).to_list()
    assert len(persisted) == 1  # no duplicate ReportRun document was created either


async def test_manual_run_is_not_blocked_by_same_day_scheduled_run(app, monkeypatch):
    sent = []
    _patch_send(monkeypatch, sent)
    window_start, window_end = previous_ist_calendar_day()

    scheduled = await run_report(
        scope=str(PROJECT), window_start=window_start, window_end=window_end, recipients=["a@example.com"], triggered_by="scheduled"
    )
    assert scheduled.status == "success"

    manual = await run_report(
        scope=str(PROJECT), window_start=window_start, window_end=window_end, recipients=["b@example.com"], triggered_by="manual"
    )
    assert manual.status == "success"
    assert len(sent) == 2

    persisted = await ReportRun.find(ReportRun.scope == str(PROJECT)).to_list()
    assert len(persisted) == 2  # scheduled + manual are two distinct, real run records
