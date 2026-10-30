from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

_EXAMPLE = {
    "condition": "asthma",
    "risk_threshold_aqi": 150,
    "pollutant": "pm2_5",
    "is_seeded": True,
    "created_at": "2026-09-01T10:00:00Z",
    "updated_at": "2026-09-01T10:00:00Z",
}


class RiskThresholdCreate(BaseModel):
    model_config = ConfigDict(json_schema_extra={"example": {k: v for k, v in _EXAMPLE.items() if k not in ("is_seeded", "created_at", "updated_at")}})

    condition: str = Field(min_length=1, max_length=100, description="Respiratory condition this threshold applies to, e.g. `asthma`.")
    risk_threshold_aqi: int = Field(gt=0, description="AQI value above which a patient with this condition is flagged at risk.")
    pollutant: str = Field(min_length=1, max_length=50, description="Pollutant the AQI reading is measured against, e.g. `pm2_5`.")


class RiskThresholdUpdate(BaseModel):
    """Partial update — every field is optional; only the ones supplied are changed."""

    model_config = ConfigDict(json_schema_extra={"example": {"risk_threshold_aqi": 160}})

    risk_threshold_aqi: int | None = Field(default=None, gt=0)
    pollutant: str | None = Field(default=None, min_length=1, max_length=50)


class RiskThresholdOut(BaseModel):
    model_config = ConfigDict(json_schema_extra={"example": _EXAMPLE})

    condition: str
    risk_threshold_aqi: int
    pollutant: str
    is_seeded: bool
    created_at: datetime
    updated_at: datetime
