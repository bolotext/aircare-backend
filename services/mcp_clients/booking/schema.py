"""Pydantic schemas for the identity-verification + teleconsultation
slot-booking API exposed by booking/router.py:

    POST /verify_identity
    GET  /next_slot
    POST /reserve_slot

These mirror the request/response contract documented in router.py's
"Response contract" comment block above verify_identity, and are wired in as
both the request body/query dependency and the response_model for each
route, so FastAPI's standard 422 validation-error body covers missing/invalid
fields on either endpoint.
"""

from typing import List, Literal, Optional

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# POST /verify_identity
# ---------------------------------------------------------------------------

class VerifyIdentityRequest(BaseModel):
    call_id: str = Field(..., min_length=1)
    patient_id: str = Field(..., min_length=1)
    spoken_dob: str = Field(..., min_length=1, description="Date of birth as spoken by the caller, in any format dateutil can parse.")


class VerifyIdentityResponse(BaseModel):
    verified: bool
    reason: Optional[str] = Field(
        None,
        description=(
            "Free-text explanation of the outcome — e.g. \"not_found\", \"mismatch\", "
            "\"lookup_failed\" on failure, or a next-step hint like \"Continue to "
            "teleconsultation slot booking\" on success. Not a fixed enum."
        ),
    )


# ---------------------------------------------------------------------------
# GET /next_slot
# ---------------------------------------------------------------------------

class NextSlotRequest(BaseModel):
    date: str = Field(..., min_length=1, description="Date to fetch available slots for, in any format dateutil can parse (e.g. YYYY-MM-DD).")


class SlotInfo(BaseModel):
    slot_id: str
    date_time: str
    doctor_name: str


class NextSlotResponse(BaseModel):
    available: bool
    slots: List[SlotInfo] = Field(default_factory=list, description="Every available slot on the requested date, earliest-first.")


# ---------------------------------------------------------------------------
# POST /reserve_slot
# ---------------------------------------------------------------------------

class ReserveSlotRequest(BaseModel):
    candidate_slot_id: str = Field(..., min_length=1, description="slot_id previously returned by /next_slot that the agent proposed to the caller.")


class ReserveSlotResponse(BaseModel):
    result: Literal["reserved", "reoffered", "no_slot"]
    slot_id: Optional[str] = None
    date_time: Optional[str] = None
    doctor_name: Optional[str] = None


# ---------------------------------------------------------------------------
# Shared error shape (502 mongo_mcp_failure / redis_mcp_failure) — router.py's
# _im_error() also merges in ad-hoc extra keys (e.g. call_id), so this is
# intentionally permissive.
# ---------------------------------------------------------------------------

class BookingErrorResponse(BaseModel):
    model_config = {"extra": "allow"}

    error: str
    reason: str
