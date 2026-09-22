import pytest
from beanie import PydanticObjectId

from reports_service.mailer import smtp_client
from reports_service.mailer.pipeline import run_report
from reports_service.mailer.smtp_client import AmbiguousSendError, TransientSendError
from reports_service.mailer.window import previous_ist_calendar_day

pytestmark = pytest.mark.asyncio

PROJECT = PydanticObjectId()


async def test_bounded_retries_then_marked_failed(app, monkeypatch):
    attempts = []

    async def always_transient_fail(message):
        attempts.append(1)
        raise TransientSendError("connection refused")

    monkeypatch.setattr(smtp_client, "_smtp_send_once", always_transient_fail)
    monkeypatch.setattr(smtp_client.asyncio, "sleep", lambda *_a, **_k: _noop())

    window_start, window_end = previous_ist_calendar_day()
    run = await run_report(
        scope=str(PROJECT), window_start=window_start, window_end=window_end, recipients=["a@example.com"], triggered_by="manual"
    )

    assert run.status == "failed"
    assert run.attempts == 3  # settings.max_smtp_attempts default
    assert len(attempts) == 3
    assert len(run.errors) == 3


async def test_ambiguous_ack_is_marked_ambiguous_and_never_retried(app, monkeypatch):
    attempts = []

    async def ambiguous_once(message):
        attempts.append(1)
        raise AmbiguousSendError("timeout waiting for ack after DATA")

    monkeypatch.setattr(smtp_client, "_smtp_send_once", ambiguous_once)

    window_start, window_end = previous_ist_calendar_day()
    run = await run_report(
        scope=str(PROJECT), window_start=window_start, window_end=window_end, recipients=["a@example.com"], triggered_by="manual"
    )

    assert run.status == "ambiguous"
    assert run.attempts == 1
    assert len(attempts) == 1  # an ambiguous outcome must never be auto-retried


async def _noop():
    return None
