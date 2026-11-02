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
