import argparse
import logging

import uvicorn

# Development: --reload restarts this single process on every source change
# in this service's own directory; never combine with --workers (uvicorn
# only supports one worker process under the reload supervisor).
# Production: --reload off, --workers a small integer (or left unset, in
# which case uvicorn falls back to the WEB_CONCURRENCY env var, else 1).


def _serve(log_level: str, reload: bool = False, workers: int | None = None) -> None:
    uvicorn.run(
        "app:app",
        host="0.0.0.0",
        port=8003,
        log_level=log_level.lower(),
        reload=reload,
        workers=None if reload else workers,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="AirCare MCP Clients service")
    parser.add_argument("command", nargs="?", default="serve", choices=["serve"])
    parser.add_argument("--log-level", default="info")
    parser.add_argument(
        "--reload",
        action="store_true",
        help="Development only: auto-restart on source changes. Do not use in production.",
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
    _serve(args.log_level, reload=args.reload, workers=args.workers)


if __name__ == "__main__":
    main()
