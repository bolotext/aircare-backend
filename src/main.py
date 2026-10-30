"""CLI for the whole backend. Run from anywhere:

    python src/main.py serve [--reload | --workers N]
    python src/main.py seed | seed-demo
    python src/main.py seed-calls --organization-id ID --project-id ID [--project-id ID ...]
    python src/main.py seed-patients --project-a-id ID --project-b-id ID
    python src/main.py seed-risk-thresholds
    python src/main.py schedule        # the daily automailer loop (separate process)

All configuration comes from the single ../.env (see common/env.py).
"""

import argparse
import asyncio
import logging
import sys
from datetime import date
from pathlib import Path

import uvicorn

_SRC_DIR = Path(__file__).resolve().parent
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))


def _serve(log_level: str, reload: bool = False, workers: int | None = None) -> None:
    """Development: `reload=True` — one process that restarts on source changes; never combine with
    `workers`. Production: `reload=False` with `workers` a small integer (or None, in which case uvicorn
    falls back to the standard WEB_CONCURRENCY env var, else 1)."""

    from gateway.config import get_settings

    settings = get_settings()
    uvicorn.run(
        "app:app",
        app_dir=str(_SRC_DIR),
        host=settings.host,
        port=settings.port,
        log_level=log_level.lower(),
        reload=reload,
        reload_dirs=[str(_SRC_DIR)] if reload else None,
        workers=None if reload else workers,
    )


def _print_mongo_config() -> None:
    from common.mongo_utils import mongo_config_summary
    from users_service.core.config import get_settings

    settings = get_settings()
    print(f"seed: {mongo_config_summary(settings.mongo_uri, settings.mongo_db_name)}")


async def _seed() -> None:
    from app import close_databases, init_databases
    from users_service.db.seed import seed_all

    await init_databases()
    await seed_all()
    await close_databases()


async def _seed_demo() -> None:
    from app import close_databases, init_databases
    from users_service.db.seed import seed_demo_data

    await init_databases()
    await seed_demo_data()
    await close_databases()


async def _seed_calls(project_ids: list[str], organization_id: str, ref_date: str | None, calls_per_project: int) -> None:
    from app import close_databases, init_databases
    from reports_service.seed.seed_calls import seed_calls

    _print_mongo_config()
    await init_databases()
    reference_day = date.fromisoformat(ref_date) if ref_date else None
    await seed_calls(project_ids, organization_id, reference_day, calls_per_project)
    await close_databases()


async def _seed_patients(project_a_id: str, project_b_id: str) -> None:
    from app import close_databases, init_databases
    from reports_service.seed.seed_patients import seed_patients

    _print_mongo_config()
    await init_databases()
    await seed_patients(project_a_id, project_b_id)
    await close_databases()


async def _seed_risk_thresholds() -> None:
    from app import close_databases, init_databases
    from reports_service.seed.seed_risk_thresholds import seed_risk_thresholds

    _print_mongo_config()
    await init_databases()
    await seed_risk_thresholds()
    await close_databases()


async def _schedule() -> None:
    from app import app, close_databases, init_databases, make_internal_client
    from reports_service.mailer.scheduler import run_scheduler_loop

    await init_databases()
    users_http_client = make_internal_client(app)
    try:
        await run_scheduler_loop(users_http_client)
    finally:
        await users_http_client.aclose()
        await close_databases()


def main() -> None:
    parser = argparse.ArgumentParser(description="Manappuram backend")
    parser.add_argument(
        "command",
        nargs="?",
        default="serve",
        choices=["serve", "seed", "seed-demo", "seed-calls", "seed-patients", "seed-risk-thresholds", "schedule"],
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
        help="serve only. Development only: auto-restart on source changes; implies a single worker.",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=None,
        help="serve only. Production worker processes (ignored with --reload). Defaults to WEB_CONCURRENCY, else 1.",
    )
    args = parser.parse_args()

    logging.basicConfig(level=args.log_level.upper())
    print(f"Starting Manappuram backend with command: {args.command}")

    if args.command == "seed":
        asyncio.run(_seed())
    elif args.command == "seed-demo":
        asyncio.run(_seed_demo())
    elif args.command == "seed-calls":
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
