import argparse
import asyncio
import logging
from pathlib import Path

import uvicorn

# .../services/users/src/users_service/__init__.py -> repo root, for
# --reload's file-watcher: it must watch this service's own src/ *and* the
# shared common lib's src/ (a plain workspace-relative "src" reload_dirs
# entry only covers the former, and only when run from services/users/ — see
# _serve below).
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
    """

    uvicorn.run(
        "users_service.app:app",
        host="0.0.0.0",
        port=45462,
        log_level=log_level.lower(),
        reload=reload,
        reload_dirs=[str(_SERVICE_SRC_DIR), str(_COMMON_SRC_DIR)] if reload else None,
        workers=None if reload else workers,
    )


async def _seed() -> None:
    from users_service.db.mongodb import close_db, get_client, init_db
    from users_service.db.seed import seed_all

    await init_db(get_client())
    await seed_all()
    await close_db()


async def _seed_demo() -> None:
    from users_service.db.mongodb import close_db, get_client, init_db
    from users_service.db.seed import seed_demo_data

    await init_db(get_client())
    await seed_demo_data()
    await close_db()


def main() -> None:
    parser = argparse.ArgumentParser(description="dummay Users microservice")
    parser.add_argument("command", nargs="?", default="serve", choices=["serve", "seed", "seed-demo"])
    parser.add_argument("--log-level", default="info")
    parser.add_argument(
        "--reload",
        action="store_true",
        help="Development only: auto-restart on source changes. Do not use in production — implies a "
        "single worker process (see --workers).",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=None,
        help="Production: number of worker processes (ignored if --reload is set). Defaults to the "
        "WEB_CONCURRENCY env var if set, else 1.",
    )
    args = parser.parse_args()

    logging.basicConfig(level=args.log_level.upper())
    print(f"Starting dummay Users microservice with command: {args.command}")
    if args.command == "seed":
        asyncio.run(_seed())
    elif args.command == "seed-demo":
        asyncio.run(_seed_demo())
    else:
        _serve(args.log_level, reload=args.reload, workers=args.workers)


if __name__ == "__main__":
    main()
