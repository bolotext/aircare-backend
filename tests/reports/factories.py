from datetime import date, datetime, timezone
from uuid import uuid4

from beanie import PydanticObjectId

from reports_service.calls.model import Call
from reports_service.core.users_client import IdentityContext
from reports_service.patient_calls.model import AqiReading, PatientCall
from reports_service.patients.model import Patient
from reports_service.risk_thresholds.model import RiskThreshold

DEFAULT_PERMISSIONS = {
    "ADMIN": frozenset({"*"}),
    "SUPERVISOR": frozenset(
        {
            "project.view",
            "report.view",
            "report.export",
            "report.configure_recipients",
            "report.trigger_manual",
            "patient.view",
            "patient.create",
            "patient.update",
            "patient.delete",
            "patient_call.view",
            "patient_call.create",
            "patient_call.update",
            "patient_call.delete",
            "s3.view",
        }
    ),
    "VIEWER": frozenset({"project.view", "report.view", "patient.view", "patient_call.view"}),
}


async def make_call(project_id: PydanticObjectId, organization_id: PydanticObjectId, **overrides) -> Call:
    now = datetime.now(timezone.utc)
    defaults = dict(
        project_id=project_id,
        organization_id=organization_id,
        conversation_id=str(uuid4()),
        bot_id="test-bot",
        language="en-IN",
        call_status="ANSWERED",
        disposition="RESOLVED",
        call_start_time=now,
        call_connected_time=now,
        call_end_time=now,
        connected_seconds=60.0,
        tool_failure_count=0,
        raw={},
    )
    defaults.update(overrides)
    call = Call(**defaults)
    await call.insert()
    return call


async def make_patient(project_id: PydanticObjectId, **overrides) -> Patient:
    defaults = dict(
        patient_id=f"PT-TEST-{uuid4().hex[:8]}",
        project_id=project_id,
        full_name="Test Patient",
        phone_number="+910000000000",
        dob=date(1990, 1, 1),
        city="Delhi",
        condition="asthma",
        preferred_language="hindi",
        risk_flag=False,
        do_not_call=False,
        is_seeded=False,
    )
    defaults.update(overrides)
    patient = Patient(**defaults)
    await patient.insert()
    return patient


async def make_risk_threshold(**overrides) -> RiskThreshold:
    defaults = dict(condition="asthma", risk_threshold_aqi=150, pollutant="pm2_5", is_seeded=False)
    defaults.update(overrides)
    threshold = RiskThreshold(**defaults)
    await threshold.insert()
    return threshold


async def make_patient_call(patient_id: str, **overrides) -> PatientCall:
    defaults = dict(
        call_id=f"call-test-{uuid4().hex[:8]}",
        patient_id=patient_id,
        phone_number="+910000000000",
        city="Delhi",
        condition="asthma",
        direction="outbound",
        live_aqi_reading=AqiReading(value=180.0, timestamp="2026-09-21T15:30:00Z", raw_response={}),
        second_aqi_reading=None,
        risk_threshold=150,
        aqi_reconciliation_delta=None,
        reading_timestamp=datetime.now(timezone.utc),
        status="APPOINTMENT_INTERESTED",
        disposition=None,
        telephony_triggered=False,
    )
    defaults.update(overrides)
    call = PatientCall(**defaults)
    await call.insert()
    return call


def make_identity(
    role: str,
    project_ids: list[str] | None = None,
    has_all_projects: bool | None = None,
    permissions: frozenset[str] | None = None,
    user_id: str = "000000000000000000000001",
    organization_id: str = "000000000000000000000002",
) -> IdentityContext:
    resolved_has_all_projects = has_all_projects if has_all_projects is not None else (role == "ADMIN")
    return IdentityContext(
        user_id=user_id,
        organization_id=organization_id,
        role=role,
        permissions=permissions if permissions is not None else DEFAULT_PERMISSIONS.get(role, frozenset()),
        project_ids=None if resolved_has_all_projects else frozenset(project_ids or []),
        has_all_projects=resolved_has_all_projects,
    )
