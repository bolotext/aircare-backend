import argparse
import asyncio
import logging
from datetime import date
from pathlib import Path

import uvicorn

# .../services/reports/src/reports_service/__init__.py -> repo root, for
# --reload's file-watcher: it must watch this service's own src/ *and* the
# shared common lib's src/ (a plain workspace-relative "src" reload_dirs
# entry only covers the former, and only when run from services/reports/ —
# see _serve below).
_REPO_ROOT = Path(__file__).resolve().parents[4]
_SERVICE_SRC_DIR = Path(__file__).resolve().parents[1]
_COMMON_SRC_DIR = _REPO_ROOT / "libs" / "common" / "src"


def _serve(log_level: str, reload: bool = False, workers: int | None = None) -> None:
    """Development: `reload=True` — a single process that restarts itself on
    every source change (this service's own src/ and the shared common lib's
    src/); never combine with `workers` (uvicorn only supports one worker
    process under the reload supervisor, and warns/ignores `workers` if both
    are set — see the CLI help for --reload/--workers).

    Production: `reload=False` (the default) with `workers` a small integer
    (or left `None`, in which case uvicorn falls back to the standard
    `WEB_CONCURRENCY` env var, else 1) — one process per CPU core is a
    reasonable starting point for a single-container deployment like this
    one's docker-compose setup, which doesn't otherwise scale via replicas.
    Doesn't apply to `schedule` (the automailer loop) — that's a separate,
    single-process, non-uvicorn command regardless of this flag.
    """

    uvicorn.run(
        "reports_service.app:app",
        host="0.0.0.0",
        port=8002,
        log_level=log_level.lower(),
        reload=reload,
        reload_dirs=[str(_SERVICE_SRC_DIR), str(_COMMON_SRC_DIR)] if reload else None,
        workers=None if reload else workers,
    )


async def _seed_calls(project_ids: list[str], organization_id: str, ref_date: str | None, calls_per_project: int) -> None:
    from reports_service.db.mongodb import close_db, get_client, init_db
    from reports_service.seed.seed_calls import seed_calls

    await init_db(get_client())
    reference_day = date.fromisoformat(ref_date) if ref_date else None
    await seed_calls(project_ids, organization_id, reference_day, calls_per_project)
    await close_db()


async def _seed_patients(project_a_id: str, project_b_id: str) -> None:
    from reports_service.db.mongodb import close_db, get_client, init_db
    from reports_service.seed.seed_patients import seed_patients

    await init_db(get_client())
    await seed_patients(project_a_id, project_b_id)
    await close_db()


async def _seed_risk_thresholds() -> None:
    from reports_service.db.mongodb import close_db, get_client, init_db
    from reports_service.seed.seed_risk_thresholds import seed_risk_thresholds

    await init_db(get_client())
    await seed_risk_thresholds()
    await close_db()


async def _schedule() -> None:
    import httpx

    from reports_service.core.config import get_settings
    from reports_service.db.mongodb import close_db, get_client, init_db
    from reports_service.mailer.scheduler import run_scheduler_loop

    await init_db(get_client())
    users_http_client = httpx.AsyncClient(base_url=get_settings().users_service_url)
    try:
        await run_scheduler_loop(users_http_client)
    finally:
        await users_http_client.aclose()
        await close_db()


def main() -> None:
    parser = argparse.ArgumentParser(description="dummay Reports microservice")
    parser.add_argument(
        "command",
        nargs="?",
        default="serve",
        choices=["serve", "seed-calls", "seed-patients", "seed-risk-thresholds", "schedule"],
    )
    parser.add_argument("--log-level", default="info")
    parser.add_argument("--project-id", dest="project_ids", action="append", default=[])
    parser.add_argument("--organization-id", dest="organization_id", default=None)
    parser.add_argument("--date", dest="ref_date", default=None, help="ISO date, default: yesterday in Asia/Kolkata")
    parser.add_argument("--calls-per-project", type=int, default=30)
    parser.add_argument("--project-a-id", dest="project_a_id", default=None, help="seed-patients only")
    parser.add_argument("--project-b-id", dest="project_b_id", default=None, help="seed-patients only")
    parser.add_argument(
        "--reload",
        action="store_true",
        help="serve only. Development only: auto-restart on source changes. Do not use in production — "
        "implies a single worker process (see --workers).",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=None,
        help="serve only. Production: number of worker processes (ignored if --reload is set). Defaults "
        "to the WEB_CONCURRENCY env var if set, else 1.",
    )
    args = parser.parse_args()

    logging.basicConfig(level=args.log_level.upper())
    print(f"Starting dummay Reports microservice with command: {args.command}")

    if args.command == "seed-calls":
        if not args.project_ids or not args.organization_id:
            parser.error("seed-calls requires at least one --project-id and an --organization-id")
        asyncio.run(_seed_calls(args.project_ids, args.organization_id, args.ref_date, args.calls_per_project))
    elif args.command == "seed-patients":
        if not args.project_a_id or not args.project_b_id:
            parser.error("seed-patients requires --project-a-id and --project-b-id")
        asyncio.run(_seed_patients(args.project_a_id, args.project_b_id))
    elif args.command == "seed-risk-thresholds":
        asyncio.run(_seed_risk_thresholds())
    elif args.command == "schedule":
        asyncio.run(_schedule())
    else:
        _serve(args.log_level, reload=args.reload, workers=args.workers)


if __name__ == "__main__":
    main()
