from datetime import datetime, timezone

from reports_service.calls.schema_adapters import _is_call_logs_shape, normalize_call_logs

EXPECTED = datetime(2026, 10, 26, 11, 31, 19, tzinfo=timezone.utc)  # 5:01:19 PM IST


def _doc(start):
    return {"_id": 1, "conversation_id": "c", "callStartTime": start, "tempDate": "26/10/2026"}


def test_plain_iso_z_string_is_used_not_tempdate_midnight():
    out = normalize_call_logs(_doc("2026-10-26T11:31:19Z"), project_id="p", organization_id="o")
    assert out["call_start_time"] == EXPECTED


def test_wrapped_isodate_and_naive_utc():
    assert normalize_call_logs(_doc("ISODate(2026-10-26T11:31:19+00:00)"), project_id="p", organization_id="o")["call_start_time"] == EXPECTED
    assert normalize_call_logs(_doc("2026-10-26T11:31:19"), project_id="p", organization_id="o")["call_start_time"] == EXPECTED


def test_shape_detected_for_plain_iso():
    assert _is_call_logs_shape(_doc("2026-10-26T11:31:19Z"))


def test_journey_entry_uses_the_sources_own_call_start_time():
    from reports_service.calls.journey import build_entry
    from reports_service.calls.model import Call

    doc = {"_id": 1, "conversation_id": "c", "callStartTime": "2026-10-26T11:31:19Z", "tempDate": "27/10/2026"}
    # An adapter that fell back to tempDate (midnight UTC of the wrong day).
    call = Call.model_validate(
        {
            "project_id": "6ab4ceafe9f5e5190f56b7f0",
            "organization_id": "6ab4ceafe9f5e5190f56b7f0",
            "conversation_id": "c",
            "bot_id": "b",
            "language": "hi",
            "call_status": "ANSWERED",
            "disposition": "X",
            "call_start_time": datetime(2026, 10, 27, tzinfo=timezone.utc),
            "tool_failure_count": 0,
            "raw": {},
        }
    )
    assert build_entry(call, doc)["call_start_time"] == "2026-10-26T11:31:19"


def test_journey_issue_messages_explain_queued_post_call():
    from reports_service.calls.journey import annotate

    def e(cid, start, seq, trigger, stage=None):
        return {"conversation_id": cid, "call_start_time": start, "sequence_number": seq, "trigger_call": trigger,
                "stage_code": stage, "next_trigger_date": None, "day": None, "attempt_today": None,
                "max_trigger_today": None, "issues": [], "scheduled_for": None, "seconds_vs_schedule": None}

    entries = [e("a", "2026-10-26T10:00:00", 4, False, "APPOINTMENT_INTERESTED"), e("b", "2026-10-26T10:30:00", 4, True)]
    annotate(entries)
    by_code = {i["code"]: i["message"] for i in entries[1]["issues"]}
    assert "queued" in by_code["duplicate_sequence_number"] and "RNR window" in by_code["duplicate_sequence_number"]
    assert "INTERESTED" in by_code["called_after_stop"] and "queued" in by_code["called_after_stop"]
