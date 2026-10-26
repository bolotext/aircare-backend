import logging

from fastapi import FastAPI

from booking.router import router as booking_router

logger = logging.getLogger("mcp_clients")

_DESCRIPTION = """
Identity verification + teleconsultation slot booking, the mid-call tool API
the voice agent calls (verify_identity, next_slot, reserve_slot — all under
/api/v1/mcp/*). Talks to MongoDB and Redis only through this service's own
MCP clients (mongo_mcp_client.py, redis_mcp_client.py) — never a direct
driver.
"""


def create_app() -> FastAPI:
    app = FastAPI(
        title="Manappuram MCP Clients Service",
        description=_DESCRIPTION,
        version="1.0.0",
        openapi_tags=[
            {"name": "booking", "description": "Mid-call identity verification and teleconsultation slot booking."},
            {"name": "health", "description": "Service liveness check."},
        ],
    )
    # booking_router itself carries the /mcp prefix; mounting it at /api/v1
    # here gives /api/v1/mcp/... so the API Gateway's proxy (which forwards
    # every request as /api/v1/<first-path-segment>/... regardless of
    # upstream) can route it via the "mcp" segment — see api-gateway's
    # app.py::_route_map.
    app.include_router(booking_router, prefix="/api/v1")

    @app.get("/health", tags=["health"], summary="Service health check")
    async def health() -> dict:
        return {"service": "mcp-clients-service"}

    return app


app = create_app()
