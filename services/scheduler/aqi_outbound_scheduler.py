"""
Aircare AQI outbound advisory sweep — scheduler service periodic task.

Runs on a fixed interval (AQI_POLL_INTERVAL_MINUTES) and, for every distinct
city with at-risk seeded patients, reconciles two independent live AQI
sources (OpenAQ + WAQI) and — only when both sources are fresh, agree within
tolerance, and the reconciled value crosses the patient's condition
threshold, and the patient is outside their callback cooldown — hands off to
initiate_outbound_advisory_call, which is a STUB (no confirmed telephony
result) that logs the decision and writes a `calls` doc with status="QUEUED".

Mongo access is direct: a single motor AsyncIOMotorClient built from
MONGO_URI, no MCP gateway, no second abstraction layer. Same collections
(patients, risk_thresholds, calls) and document shapes as
services/reports/src/reports_service/{patients,risk_thresholds,patient_calls}/model.py,
so this sweep's writes render on the Reports dashboard unchanged — but
written as plain dicts here rather than importing those Beanie models
directly, since this is a separate deployable service and shouldn't import
another service's package.

Run with (from the project root, with services/scheduler/.env loaded):
    python services/scheduler/aqi_outbound_scheduler.py

Config (.env, os.getenv only — never hardcoded, never logged)
---------------------------------------------------------------
MONGO_URI                     Mongo connection string (default mongodb://localhost:27017,
                               matching docker-compose.yml's mongo service). Never logged.
MONGO_DB_NAME                 Mongo database name (default "aircare").
AQI_POLL_INTERVAL_MINUTES     Sweep interval, in minutes (default 30).
AQI_FRESHNESS_WINDOW_MINUTES  Max accepted AQI reading age, in minutes (default 60).
AQI_TOLERANCE                 Max allowed delta (AQI points) between OpenAQ and WAQI
                               before they're treated as disagreeing (default 20).
CALLBACK_COOLDOWN_HOURS       Per-patient re-advisory cooldown, in hours (default 24).
OPENAQ_API_KEY                Sent as X-API-Key to OpenAQ. Never logged.
WAQI_API_TOKEN                Sent as the WAQI `token` query param. Never logged; falls
                               back to WAQI_TOKEN if unset.
INYA_API_BASE_URL             Inya Platform base URL (default https://api.inya.ai/platform/v1).
INYA_BOT_ID / INYA_API_KEY    Inya outbound-call credentials. Never logged.
INYA_CALL_ENVIRONMENT         Omitted from the request when unset.
LOG_LEVEL                     Python logging level (default INFO).

NOTE (DPDP / PII): patients.city and phone numbers are sensitive personal
data. This module never logs a phone number or MONGO_URI — patient_id is
used for log correlation instead.
"""

import asyncio
import os
import uuid
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import httpx
from dotenv import load_dotenv
from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorDatabase
from pymongo.errors import PyMongoError
from common.log_utils import setup_logger

logger = setup_logger(__name__)

load_dotenv()  # services/scheduler/.env
# ---------------------------------------------------------------------------
# Config (os.getenv only — never hardcoded, never logged)
# ---------------------------------------------------------------------------
AQI_POLL_INTERVAL_MINUTES = float(os.getenv("AQI_POLL_INTERVAL_MINUTES", "30"))
AQI_FRESHNESS_WINDOW_MINUTES = float(os.getenv("AQI_FRESHNESS_WINDOW_MINUTES", "60"))
AQI_TOLERANCE = float(os.getenv("AQI_TOLERANCE", "20"))
CALLBACK_COOLDOWN_HOURS = float(os.getenv("CALLBACK_COOLDOWN_HOURS", "24"))

# Read once, held only in-process, never printed/logged.
_OPENAQ_API_KEY = os.getenv("OPENAQ_API_KEY")
# WAQI_API_TOKEN is this task's own name; WAQI_TOKEN is accepted as a fallback
# for the same free aqicn.org token under a different name.
_WAQI_API_TOKEN = os.getenv("WAQI_API_TOKEN") or os.getenv("WAQI_TOKEN")

INYA_API_BASE_URL = os.getenv("INYA_API_BASE_URL", "https://api.inya.ai/platform/v1").rstrip("/")
INYA_BOT_ID = os.getenv("INYA_BOT_ID")
_INYA_API_KEY = os.getenv("INYA_API_KEY")
INYA_CALL_ENVIRONMENT = os.getenv("INYA_CALL_ENVIRONMENT")  # omitted from the request when unset

HTTP_TIMEOUT_SECONDS = 5.0
PATIENT_FETCH_LIMIT = int(os.getenv("AQI_PATIENT_FETCH_LIMIT", "1000"))
DEFAULT_RISK_THRESHOLD_AQI = 150.0  # used only if a condition has no risk_thresholds doc

# OpenAQ v3's /v3/locations has no free-text "city" filter (that was a v1/v2
# param) — passing city= is silently ignored and returns arbitrary global
# results instead of the requested city. v3 requires coordinates+radius, so
# seeded patient cities are matched against known coordinates instead.
_CITY_COORDINATES = {
    "delhi": (28.6139, 77.2090),
    "mumbai": (19.0760, 72.8777),
    "bengaluru": (12.9716, 77.5946),
    "bangalore": (12.9716, 77.5946),
    "hyderabad": (17.3850, 78.4867),
    "kolkata": (22.5726, 88.3639),
    "chennai": (13.0827, 80.2707),
}
_OPENAQ_SEARCH_RADIUS_METERS = 25000

# US EPA PM2.5 (24-hr) AQI breakpoints: (conc_lo, conc_hi, aqi_lo, aqi_hi), µg/m³.
# risk_thresholds is only seeded for pollutant "pm2_5", so this is the one
# conversion needed to make OpenAQ's raw concentration comparable to WAQI's
# already-computed AQI index (and to risk_threshold_aqi).
_PM25_AQI_BREAKPOINTS = [
    (0.0, 12.0, 0, 50),
    (12.1, 35.4, 51, 100),
    (35.5, 55.4, 101, 150),
    (55.5, 150.4, 151, 200),
    (150.5, 250.4, 201, 300),
    (250.5, 350.4, 301, 400),
    (350.5, 500.4, 401, 500),
]


def _object_id(value):
    """Wrap a 24-char hex ObjectId string as Mongo extended JSON — mirrors
    scheduler/seed.py's _object_id() and ISODate()'s {"$date": ...} pattern
    (plugins/project/routes.py), the shape the Mongo MCP server's EJSON
    parsing turns into a real BSON ObjectId on insert. patient["project_id"]
    fetched back via mcp_find already comes across in this same {"$oid": ...}
    shape (that's how the MCP server's find tool serializes an ObjectId
    field), so this passes it through unchanged rather than re-wrapping it —
    double-wrapping ({"$oid": {"$oid": ...}}) is the same class of
    insert-rejection bug double-wrapped ISODate() values have hit before."""
    if not value:
        return None
    if isinstance(value, dict) and "$oid" in value:
        return value
    return {"$oid": value}

def _pm25_concentration_to_aqi(concentration_ugm3: float) -> float:
    """US EPA PM2.5 24-hr breakpoint conversion. Concentrations outside the
    table clamp to the nearest boundary AQI rather than erroring."""
    c = round(concentration_ugm3, 1)
    if c <= _PM25_AQI_BREAKPOINTS[0][0]:
        return float(_PM25_AQI_BREAKPOINTS[0][2])
    if c > _PM25_AQI_BREAKPOINTS[-1][1]:
        return float(_PM25_AQI_BREAKPOINTS[-1][3])
    for lo, hi, aqi_lo, aqi_hi in _PM25_AQI_BREAKPOINTS:
        if lo <= c <= hi:
            return ((aqi_hi - aqi_lo) / (hi - lo)) * (c - lo) + aqi_lo
    return float(_PM25_AQI_BREAKPOINTS[-1][3])  # unreachable given the bounds checks above


DISPOSITION_COOLDOWN_SKIPPED = "CALLBACK_COOLDOWN_ACTIVE_SKIPPED"
DISPOSITION_ADVISORY_INITIATED = "ADVISORY_CALL_INITIATED_MOCK"
DISPOSITION_CITY_ERROR = "CITY_PROCESSING_ERROR"
DISPOSITION_BELOW_THRESHOLD = "AQI_BELOW_RISK_THRESHOLD_SKIPPED"
DISPOSITION_DISAGREED = "AQI_SOURCES_DISAGREED_SKIPPED"
DISPOSITION_INCONCLUSIVE = "AQI_READING_INCONCLUSIVE_SKIPPED"

# A `calls` doc this sweep itself wrote (see initiate_outbound_advisory_call)
# counts as still outstanding until something moves it off "QUEUED" — there's
# no larger terminal-status vocabulary yet since nothing else sets one.
_PENDING_CALL_STATUS = "QUEUED"


def _is_pending_call(doc: dict) -> bool:
    return doc.get("status") == _PENDING_CALL_STATUS


# ---------------------------------------------------------------------------
# Mongo — a single motor client built from MONGO_URI, no ODM: this service
# only ever reads two collections (patients, risk_thresholds) and writes one
# (calls), all as plain dicts matching the Reports service's Beanie schemas.
# ---------------------------------------------------------------------------
_MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
_MONGO_DB_NAME = os.getenv("MONGO_DB_NAME", "aircare")

_client: AsyncIOMotorClient | None = None


def get_db() -> AsyncIOMotorDatabase:
    global _client
    if _client is None:
        _client = AsyncIOMotorClient(_MONGO_URI)
    return _client[_MONGO_DB_NAME]


def close_db() -> None:
    global _client
    if _client is not None:
        _client.close()
        _client = None


async def _find(collection: str, query: dict, limit: int | None = None, sort: dict | None = None) -> list[dict]:
    cursor = get_db()[collection].find(query)
    if sort:
        cursor = cursor.sort(list(sort.items()))
    if limit:
        cursor = cursor.limit(limit)
    return await cursor.to_list(length=limit)


async def _insert_one(collection: str, document: dict) -> None:
    await get_db()[collection].insert_one(document)


def _safe_parse_dt(value) -> datetime | None:
    """Accepts a native datetime (as Mongo/motor returns it) or an ISO-8601
    string (as OpenAQ/WAQI return it). Naive values are treated as UTC."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    return None


# ---------------------------------------------------------------------------
# AQI source readings — bounded timeout, never raise, {value, timestamp,
# raw_response} or None on failure/timeout.
# ---------------------------------------------------------------------------
async def fetch_openaq_reading(city: str) -> dict | None:
    coordinates = _CITY_COORDINATES.get((city or "").strip().lower())
    if not coordinates:
        logger.warning("fetch_openaq_reading: no known coordinates for city=%s", city)
        return None
    headers = {"X-API-Key": _OPENAQ_API_KEY} if _OPENAQ_API_KEY else {}
    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_SECONDS) as client:
            loc_resp = await client.get(
                "https://api.openaq.org/v3/locations",
                params={
                    "coordinates": f"{coordinates[0]},{coordinates[1]}",
                    "radius": _OPENAQ_SEARCH_RADIUS_METERS,
                    "limit": 10,
                },
                headers=headers,
            )
            loc_resp.raise_for_status()
            locations = loc_resp.json().get("results", [])
            if not locations:
                return None
            # nearest isn't necessarily freshest — prefer whichever candidate
            # station in range last reported a measurement
            locations.sort(key=lambda loc: (loc.get("datetimeLast") or {}).get("utc") or "", reverse=True)

            for location in locations:
                # /latest returns one entry per sensor (every pollutant + met
                # variable) with no parameter name on the entry itself — the
                # sensor->pollutant mapping only exists on the location's own
                # `sensors` list, so pm25 has to be resolved here first.
                pm25_sensor_ids = {
                    s["id"] for s in location.get("sensors", [])
                    if (s.get("parameter") or {}).get("name") == "pm25"
                }
                if not pm25_sensor_ids:
                    continue

                meas_resp = await client.get(
                    f"https://api.openaq.org/v3/locations/{location['id']}/latest",
                    headers=headers,
                )
                meas_resp.raise_for_status()
                pm25_readings = [
                    r for r in meas_resp.json().get("results", [])
                    if r.get("sensorsId") in pm25_sensor_ids
                ]
                if not pm25_readings:
                    continue
                pm25_readings.sort(key=lambda r: (r.get("datetime") or {}).get("utc") or "", reverse=True)
                reading = pm25_readings[0]
                concentration_ugm3 = float(reading["value"])
                timestamp = (reading.get("datetime") or {}).get("utc")

                return {
                    "value": _pm25_concentration_to_aqi(concentration_ugm3),
                    "timestamp": timestamp,
                    "raw_response": {
                        "pm25_concentration_ugm3": concentration_ugm3,
                        "location_id": location["id"],
                        "location_name": location.get("name"),
                        "sensor_id": reading.get("sensorsId"),
                    },
                }
            return None  # no in-range location had a usable pm25 sensor reading
    except (httpx.HTTPError, KeyError, ValueError, TypeError) as e:
        logger.warning("fetch_openaq_reading failed for city=%s: %s", city, e)
        return None


async def fetch_waqi_reading(city: str) -> dict | None:
    if not _WAQI_API_TOKEN:
        logger.warning("fetch_waqi_reading skipped for city=%s: no WAQI token configured", city)
        return None
    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_SECONDS) as client:
            resp = await client.get(f"https://api.waqi.info/feed/{city}/", params={"token": _WAQI_API_TOKEN})
        resp.raise_for_status()
        payload = resp.json()
        if payload.get("status") != "ok":
            return None
        data = payload["data"]
        timestamp = data.get("time", {}).get("iso") if isinstance(data.get("time"), dict) else None
        return {"value": float(data["aqi"]), "timestamp": timestamp, "raw_response": data}
    except (httpx.HTTPError, KeyError, ValueError, TypeError) as e:
        # never log resp.url here — it carries the token as a query param
        logger.warning("fetch_waqi_reading failed for city=%s: %s", city, e)
        return None


def _is_fresh(reading: dict) -> bool:
    observed_at = _safe_parse_dt(reading.get("timestamp"))
    if not observed_at:
        return False
    age_minutes = (datetime.now(timezone.utc) - observed_at).total_seconds() / 60.0
    return 0 <= age_minutes <= AQI_FRESHNESS_WINDOW_MINUTES


def _reconcile(reading_a: dict | None, reading_b: dict | None):
    """Returns ("ok", reconciled_value, delta) | ("inconclusive", None, None) |
    ("disagreed", None, delta). Never averages; conservative = max of the two."""
    if not reading_a or not reading_b:
        logger.warning("AQI reconciliation inconclusive due to missing reading: a=%s b=%s", reading_a, reading_b)
        return ("inconclusive", None, None)
    if not _is_fresh(reading_a) or not _is_fresh(reading_b):
        logger.warning("AQI reconciliation inconclusive due to staleness: a=%s b=%s", reading_a, reading_b)
        return ("inconclusive", None, None)
    delta = abs(reading_a["value"] - reading_b["value"])
    logger.debug("[AQI reconciliation] delta=%s AQI_TOLERANCE=%s", delta, AQI_TOLERANCE)
    if delta > AQI_TOLERANCE:
        logger.error("AQI reconciliation disagreed: delta=%s > AQI_TOLERANCE=%s", delta, AQI_TOLERANCE)
        return ("disagreed", None, delta)
    return ("ok", max(reading_a["value"], reading_b["value"]), delta)


# ---------------------------------------------------------------------------
# Mongo lookups
# ---------------------------------------------------------------------------
async def _fetch_at_risk_patients_by_city() -> dict[str, list[dict]]:
    """One find() against patients, grouped in-process by city."""
    patients = await _find(
        "patients",
        {"is_seeded": True, "risk_flag": True, "do_not_call": {"$ne": True}},
        limit=PATIENT_FETCH_LIMIT,
    )
    by_city = defaultdict(list)
    for patient in patients:
        by_city[patient.get("city")].append(patient)
    return by_city


async def _get_risk_threshold(condition: str, threshold_cache: dict) -> float:
    if condition in threshold_cache:
        return threshold_cache[condition]
    docs = await _find("risk_thresholds", {"condition": condition}, limit=1)
    doc = docs[0] if docs else {}
    # dummy-seeded field is risk_threshold_aqi; accept threshold_value too in
    # case a differently-shaped risk_thresholds doc is seeded.
    threshold = doc.get("risk_threshold_aqi", doc.get("threshold_value", DEFAULT_RISK_THRESHOLD_AQI))
    threshold_cache[condition] = threshold
    return threshold


def _in_cooldown(patient: dict) -> bool:
    last_episode = _safe_parse_dt(patient.get("last_advisory_episode"))
    if not last_episode:
        return False
    return (datetime.now(timezone.utc) - last_episode) < timedelta(hours=CALLBACK_COOLDOWN_HOURS)


def _earliest_reading_timestamp(aqi_a: dict | None, aqi_b: dict | None) -> datetime:
    """The reconciled reading is only as fresh as its stalest input — use the
    older of the two source timestamps, falling back to now if neither
    parses (shouldn't happen: _reconcile already required both fresh)."""
    candidates = [
        t for t in (_safe_parse_dt((aqi_a or {}).get("timestamp")), _safe_parse_dt((aqi_b or {}).get("timestamp")))
        if t is not None
    ]
    return min(candidates) if candidates else datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Real outbound telephony leg — Inya Platform Trigger Call API.
# POST {INYA_API_BASE_URL}/agents/{INYA_BOT_ID}/trigger_call
# See docs.gnani.ai/Platform/Trigger_Call.md for the full contract.
#
# Only India-format (+91) numbers are split with confidence — this repo's
# seeded patients are all Indian (see _CITY_COORDINATES), and a wrong
# countryCode/phone split would dial a different, real person rather than
# erroring, so anything else is rejected rather than guessed.
# ---------------------------------------------------------------------------
_INDIA_COUNTRY_CODE = "91"


def _split_indian_e164_phone(phone_number: str) -> tuple[str | None, str | None]:
    digits = (phone_number or "").strip()
    if not digits.startswith("+" + _INDIA_COUNTRY_CODE):
        return None, None
    national_number = digits[1 + len(_INDIA_COUNTRY_CODE):]
    if len(national_number) != 10 or not national_number.isdigit():
        return None, None
    return _INDIA_COUNTRY_CODE, national_number


async def trigger_inya_outbound_call(phone_number: str, patient_id: str, client_reference_id: str | None = None) -> dict | None:
    """Places the real outbound call. Never raises: a telephony-side failure
    must not stop the `calls` doc (the identity-verification resume context)
    from being written — see initiate_outbound_advisory_call.
    Returns the parsed response body on HTTP 200, else None.
    """
    if not (_INYA_API_KEY and INYA_BOT_ID):
        logger.warning("trigger_inya_outbound_call skipped: INYA_API_KEY/INYA_BOT_ID not configured")
        return None

    country_code, national_number = _split_indian_e164_phone(phone_number)
    if not national_number:
        logger.warning("trigger_inya_outbound_call skipped patient_id=%s: phone_number is not a recognized +91 E.164 number", patient_id)
        return None

    params = {"environment": INYA_CALL_ENVIRONMENT} if INYA_CALL_ENVIRONMENT else {}
    # `name` is only a conversation-log label on Inya's side (not used for
    # identity) — patient_id avoids sending an actual name to a third-party
    # platform.
    body = {"phone": national_number, "countryCode": f"+{country_code}", "name": patient_id}
    if client_reference_id:
        body["clientReferenceId"] = client_reference_id

    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_SECONDS) as client:
            resp = await client.post(
                f"{INYA_API_BASE_URL}/agents/{INYA_BOT_ID}/trigger_call",
                params=params,
                json=body,
                headers={"x-api-key": _INYA_API_KEY, "Content-Type": "application/json"},
            )
        resp.raise_for_status()
        payload = resp.json()
        logger.info("trigger_inya_outbound_call succeeded patient_id=%s requestId=%s", patient_id, payload.get("requestId"))
        return payload
    except (httpx.HTTPError, ValueError) as e:
        # never log body/params here: phone number and name are in the
        # request body, not just the URL
        logger.warning("trigger_inya_outbound_call failed patient_id=%s: %s", patient_id, e)
        return None


# ---------------------------------------------------------------------------
# Outbound advisory — writes a `calls` doc with status="QUEUED" regardless of
# whether trigger_inya_outbound_call actually succeeded, so the attempt is
# still visible even though nothing downstream (yet) confirms it was placed.
# Same physical `calls` collection the Reports service's PatientCall model
# reads (services/reports/src/reports_service/patient_calls/model.py) — a
# future call-status webhook can update this same doc's status by call_id.
#
# Dedup: the sweep re-runs every AQI_POLL_INTERVAL_MINUTES, so without a
# check here the same at-risk patient would get a fresh `calls` doc (and a
# duplicate real outbound call) every cycle until something moves their
# QUEUED doc to some other status — skip both the call and the insert if a
# QUEUED one already exists for this phone_number.
# ---------------------------------------------------------------------------
async def initiate_outbound_advisory_call(patient: dict, reconciled_value: float, aqi_a: dict, aqi_b: dict, delta: float) -> None:
    phone_number = patient.get("phone_number")
    patient_id = patient.get("patient_id")
    patient_name = patient.get("full_name", None) or "Unknown"
    try:
        existing_calls = await _find("calls", {"phone_number": phone_number}, limit=5, sort={"created_at": -1})
        if any(_is_pending_call(doc) for doc in existing_calls):
            logger.info("initiate_outbound_advisory_call: skipping, a pending call already exists for patient_id=%s", patient_id)
            return

        client_reference_id = str(uuid.uuid4())
        logger.info("initiate_outbound_advisory_call: initiating call for patient_id=%s", patient_id)
        telephony_response = await trigger_inya_outbound_call(phone_number, patient_id, client_reference_id)

        now = datetime.now(timezone.utc)
        await _insert_one(
            "calls",
            {
                "call_id": client_reference_id,
                "patient_id": patient_id,
                "patient_name": patient_name,
                "project_id": patient.get("project_id"),
                "phone_number": phone_number,
                "city": patient.get("city"),
                "condition": patient.get("condition"),
                "direction": "outbound",
                "live_aqi_reading": aqi_a,
                "second_aqi_reading": aqi_b,
                "risk_threshold": patient.get("risk_threshold"),
                "aqi_reconciliation_delta": delta,
                "reading_timestamp": _earliest_reading_timestamp(aqi_a, aqi_b),
                "status": _PENDING_CALL_STATUS,
                "telephony_triggered": telephony_response is not None,
                "created_at": now,
                "updated_at": now,
            },
        )
        logger.info("initiate_outbound_advisory_call: calls QUEUED insert succeeded for patient_id=%s", patient_id)
    except PyMongoError:
        logger.exception("initiate_outbound_advisory_call: calls insert failed for patient_id=%s", patient_id)


# ---------------------------------------------------------------------------
# Sweep
# ---------------------------------------------------------------------------
async def _process_patient(patient: dict, city: str, reconciled_value: float, reading_a: dict, reading_b: dict, delta: float, threshold_cache: dict, counts: dict) -> None:
    patient_id = patient.get("patient_id")
    threshold = await _get_risk_threshold(patient.get("condition"), threshold_cache)

    logger.info("Processing patient_id=%s city=%s reconciled_value=%s threshold=%s", patient_id, city, reconciled_value, threshold)

    if reconciled_value < threshold:
        counts[DISPOSITION_BELOW_THRESHOLD] += 1
        logger.info(
            "%s patient_id=%s city=%s reconciled_value=%s threshold=%s",
            DISPOSITION_BELOW_THRESHOLD, patient_id, city, reconciled_value, threshold,
        )
        return

    if _in_cooldown(patient):
        # skip silently, per spec — tallied for the dashboard but not logged loudly
        counts[DISPOSITION_COOLDOWN_SKIPPED] += 1
        return

    patient_with_threshold = {**patient, "risk_threshold": threshold}
    await initiate_outbound_advisory_call(patient_with_threshold, reconciled_value, reading_a, reading_b, delta)
    counts[DISPOSITION_ADVISORY_INITIATED] += 1


async def _process_city(city: str, patients: list[dict], threshold_cache: dict, counts: dict) -> None:
    logger.info("Processing city=%s with %s patients", city, len(patients))
    reading_a = await fetch_openaq_reading(city)
    reading_b = await fetch_waqi_reading(city)
    logger.debug("City %s readings: OpenAQ=%s WAQI=%s", city, reading_a, reading_b)
    status, reconciled_value, delta = _reconcile(reading_a, reading_b)
    logger.info("City %s AQI reconciliation status=%s reconciled_value=%s delta=%s", city, status, reconciled_value, delta)

    if status == "inconclusive":
        counts[DISPOSITION_INCONCLUSIVE] += len(patients)
        return

    if status == "disagreed":
        counts[DISPOSITION_DISAGREED] += len(patients)
        return

    # readings fetched and cached once above — never refetched per patient
    for patient in patients:
        await _process_patient(patient, city, reconciled_value, reading_a, reading_b, delta, threshold_cache, counts)


async def run_aqi_advisory_sweep() -> dict:
    logger.info("run_aqi_advisory_sweep started AQI_POLL_INTERVAL_MINUTES=%s", AQI_POLL_INTERVAL_MINUTES)
    run_id = str(uuid.uuid4())
    counts: dict = defaultdict(int)
    threshold_cache: dict = {}

    by_city = await _fetch_at_risk_patients_by_city()
    counts["cities_considered"] = len(by_city)
    counts["patients_considered"] = sum(len(v) for v in by_city.values())

    for city, patients in by_city.items():
        try:
            await _process_city(city, patients, threshold_cache, counts)
        except Exception:
            # one city's failure must never kill the whole sweep
            counts[DISPOSITION_CITY_ERROR] += 1
            logger.exception("run_aqi_advisory_sweep: city processing failed city=%s", city)

    logger.info("run_aqi_advisory_sweep run_id=%s finished counts=%s", run_id, dict(counts))
    return dict(counts)


async def run_scheduler_loop() -> None:
    logger.info("AQI outbound scheduler started: poll_interval_minutes=%s", AQI_POLL_INTERVAL_MINUTES)
    while True:
        try:
            await run_aqi_advisory_sweep()
        except Exception:
            # a single failed sweep must never kill the loop
            logger.exception("run_scheduler_loop: sweep failed")
        await asyncio.sleep(AQI_POLL_INTERVAL_MINUTES * 60)


async def _main() -> None:
    try:
        await run_scheduler_loop()
    finally:
        close_db()


if __name__ == "__main__":
    asyncio.run(_main())
