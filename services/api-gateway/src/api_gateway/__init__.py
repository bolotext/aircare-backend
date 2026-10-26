import argparse
import logging
from pathlib import Path

import uvicorn

# .../services/api-gateway/src/api_gateway/__init__.py -> repo root, for
# --reload's file-watcher: it must watch this service's own src/ *and* the
# shared common lib's src/ (a plain workspace-relative "src" reload_dirs
# entry only covers the former, and only when run from services/api-gateway/
# — see main() below).
_REPO_ROOT = Path(__file__).resolve().parents[4]
_SERVICE_SRC_DIR = Path(__file__).resolve().parents[1]
_COMMON_SRC_DIR = _REPO_ROOT / "libs" / "common" / "src"


def main() -> None:
    parser = argparse.ArgumentParser(description="dummay API Gateway")
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
    uvicorn.run(
        "api_gateway.app:app",
        host="0.0.0.0",
        port=45461,
        log_level=args.log_level.lower(),
        reload=args.reload,
        reload_dirs=[str(_SERVICE_SRC_DIR), str(_COMMON_SRC_DIR)] if args.reload else None,
        workers=None if args.reload else args.workers,
    )


if __name__ == "__main__":
    main()
