"""Proxies a call's audio recording from the internal audio server so the
browser never talks to that (unauthenticated, private-network-only) service
directly — access stays gated by this service's own project RBAC, the same
way GET /calls/{conversation_id} is (see calls.router.get_call_recording).

Recording lookup needs three fields from the project's *raw* source
document — `flow_id`, the call's `YYYY_MM_DD` day bucket, and `call_uid` —
that GET /calls/{conversation_id} never returns to the frontend
(calls.schema_adapters strips the `rec_path` field these three reconstruct,
as PII — see its module docstring). They're read here directly off the raw
document and never surfaced to the frontend as fields of their own; the
frontend only ever gets back audio bytes from this proxy route.

Only applies to a project whose configured collection needs a schema
adapter (report_llm/call_logs/aircare_calls-shaped) — that's the only kind
of source observed to carry a recording at all. A native-Call-schema
project (seed/demo data) has no adapter and no known recording.
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import timezone

import httpx
from common.errors import BadGatewayError
from motor.motor_asyncio import AsyncIOMotorCollection

from reports_service.calls import repository as call_repo
from reports_service.core.config import get_settings


@dataclass(frozen=True)
class RecordingRef:
    flow_id: str
    call_date: str  # "YYYY_MM_DD" — the audio server's own day-bucket format.
    call_uid: str


async def resolve_recording_ref(
    collection: AsyncIOMotorCollection,
    adapter: Callable[..., dict] | None,
    project_id: str,
    organization_id: str,
    conversation_id: str,
) -> RecordingRef | None:
    if adapter is None:
        return None

    result = await call_repo.get_with_raw_source_by_conversation_id(
        conversation_id, project_id, collection=collection, adapter=adapter, organization_id=organization_id
    )
    if result is None:
        return None
    call, raw_doc = result

    flow_id = raw_doc.get("flow_id")
    call_uid = raw_doc.get("call_uid") or raw_doc.get("conversation_id")
    if not flow_id or not call_uid:
        return None

    # The recording file's day bucket, reliably derived from the already-
    # normalized call_start_time (tz-aware UTC — see schema_adapters'
    # per-source timestamp parsing) rather than re-parsing whichever of
    # report_llm's "YYYY-MM-DD" / call_logs's & aircare_calls' "DD/MM/YYYY"
    # `tempDate` format this particular raw document happens to use.
    call_date = call.call_start_time.astimezone(timezone.utc).strftime("%Y_%m_%d")

    return RecordingRef(flow_id=str(flow_id), call_date=call_date, call_uid=str(call_uid))


async def fetch_recording_audio(ref: RecordingRef) -> tuple[bytes, str]:
    """Fetches the raw audio bytes for `ref` from the internal audio server
    (server-to-server only — see module docstring). Raises BadGatewayError
    (surfaced as a 502) on any transport failure or non-200 response,
    consistent with how this service already treats an unreachable/erroring
    internal dependency (see core.users_client).
    """

    settings = get_settings()
    if not settings.audio_server_url:
        raise BadGatewayError("Recording playback is not configured")

    payload = {"flow_id": ref.flow_id, "call_date": ref.call_date, "call_uid": ref.call_uid}
    async with httpx.AsyncClient(timeout=settings.audio_server_timeout_seconds) as client:
        try:
            response = await client.post(settings.audio_server_url, json=payload)
        except httpx.HTTPError as exc:
            raise BadGatewayError("Unable to reach the recording server") from exc

    if response.status_code != 200:
        raise BadGatewayError("Recording server returned an unexpected response")

    content_type = response.headers.get("content-type") or "audio/wav"
    return response.content, content_type
