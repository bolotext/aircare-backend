"""
Redis MCP runtime client — the only path from this codebase into Redis via
the Redis MCP server (redis/mcp-redis, PyPI package `redis-mcp-server`,
launched as a subprocess over stdio, normally via `uvx --from redis-mcp-server
redis-mcp-server`). Scoped to exactly the handful of list/string operations
the teleconsultation slot-booking flow needs: redis_rpush, redis_lpop,
redis_lrange, redis_llen, redis_get, redis_set.

Why these primitives: `lpop`/`rpop` are single, atomic Redis commands, so a
FIFO list of open slots (see plugins/project/routes.py's slot-booking
section) gives lock-free "claim the next available slot" semantics for free
— no separate compare-and-swap dance needed. This server's own `set` tool has
no NX flag, so it is never used here to implement a reservation lock.

Connection / secrets
---------------------
This server's CLI (`redis-mcp-server`, see its main.py) always applies
click's own defaults for --host/--port/--db/--ssl even when they're not
explicitly passed, which would silently override host/port env vars — so
those (non-secret) values ARE passed as CLI args here, parsed from
REDIS_MCP_URL. Credentials are a different story: --password/--url would
land in argv (visible to anything that can read this host's process list),
so this module never passes them as CLI args. Instead REDIS_USERNAME/REDIS_PWD
are exported only into the subprocess's own environment — the server's own
config.py reads those directly, and set_redis_config_from_cli only overwrites
the REDIS_CFG keys actually present in the CLI args dict, so an
env-supplied password is left untouched as long as --password is never
passed. REDIS_MCP_URL itself is never logged, in whole or in part.

Key-prefix allow-list
-----------------------
No caller can touch an arbitrary Redis key — every key must start with one
of ALLOWED_KEY_PREFIXES, enforced here (mirrors mongo_mcp_client's
ALLOWED_COLLECTIONS discipline).

Required env vars (.env, os.getenv only — never hardcoded, never logged)
--------------------------------------------------------------------------
REDIS_MCP_URL              redis://[user:pass@]host:port[/db] (or rediss:// for TLS).
REDIS_MCP_SERVER_COMMAND    Launch command (default: uvx).
REDIS_MCP_SERVER_ARGS       Launch args (default: "--from redis-mcp-server redis-mcp-server").
MCP_CALL_TIMEOUT_SECONDS    Bounded per-call timeout, seconds (default: 3).

Construction is lazy (see _get_client) — importing this module never spawns
the subprocess, so tests can monkeypatch the redis_* functions freely.
"""

import asyncio
import json
import logging
import os
import threading
import time
import urllib.parse
from contextlib import AsyncExitStack
from uuid import uuid4

from dotenv import load_dotenv
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

load_dotenv()

logger = logging.getLogger("redis_mcp_client")

ALLOWED_KEY_PREFIXES = ("slots:", "slot_reservation:")

MCP_CALL_TIMEOUT_SECONDS = float(os.getenv("MCP_CALL_TIMEOUT_SECONDS", "3"))


class RedisMCPError(RuntimeError):
    """A definitive (non-ambiguous) Redis MCP failure — safe to treat as failed."""


def _parse_redis_url(url):
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("redis", "rediss"):
        raise RuntimeError(f"REDIS_MCP_URL has an unsupported scheme: {parsed.scheme!r}")
    db = 0
    if parsed.path and parsed.path != "/":
        try:
            db = int(parsed.path.lstrip("/"))
        except ValueError:
            db = 0
    return {
        "host": parsed.hostname or "127.0.0.1",
        "port": parsed.port or 6379,
        "db": db,
        "ssl": parsed.scheme == "rediss",
        "username": parsed.username,
        "password": parsed.password,
    }


class _RedisMCPClient:
    def __init__(self):
        redis_url = os.getenv("REDIS_MCP_URL")
        if not redis_url:
            raise RuntimeError("REDIS_MCP_URL is not set")
        self._conn = _parse_redis_url(redis_url)

        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()

        self._exit_stack = None
        self._session = None
        self._tool_names = set()

        fut = asyncio.run_coroutine_threadsafe(self._async_init(), self._loop)
        fut.result(timeout=30)
        logger.info("RedisMCPClient ready: tools=%s host=%s port=%s db=%s", sorted(self._tool_names), self._conn["host"], self._conn["port"], self._conn["db"])

    def _run_loop(self):
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    async def _async_init(self):
        command = os.getenv("REDIS_MCP_SERVER_COMMAND", "uvx")
        base_args = os.getenv("REDIS_MCP_SERVER_ARGS", "--from redis-mcp-server redis-mcp-server").split()
        # host/port/db are not secret — safe as CLI args. Credentials never
        # appear here (see module docstring); they travel only via env.
        conn_args = ["--host", str(self._conn["host"]), "--port", str(self._conn["port"]), "--db", str(self._conn["db"])]
        if self._conn["ssl"]:
            conn_args.append("--ssl")
        args = base_args + conn_args

        spawn_env = {}
        if self._conn["username"]:
            spawn_env["REDIS_USERNAME"] = self._conn["username"]
        if self._conn["password"]:
            spawn_env["REDIS_PWD"] = self._conn["password"]

        server_params = StdioServerParameters(command=command, args=args, env=spawn_env or None)

        self._exit_stack = AsyncExitStack()
        read, write = await self._exit_stack.enter_async_context(stdio_client(server_params))
        self._session = await self._exit_stack.enter_async_context(ClientSession(read, write))
        await self._session.initialize()

        tools = (await self._session.list_tools()).tools
        self._tool_names = {t.name for t in tools}
        logger.info("Redis MCP discovered tools: %s", sorted(self._tool_names))

        required = {"rpush", "lpop", "lrange", "llen", "get", "set"}
        missing = required - self._tool_names
        if missing:
            raise RuntimeError(f"Required Redis MCP tool(s) {sorted(missing)} not found. Discovered: {sorted(self._tool_names)}")

    def _run_coro(self, coro, timeout):
        fut = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return fut.result(timeout=timeout)

    @staticmethod
    def _validate_key(key):
        if not any(key.startswith(p) for p in ALLOWED_KEY_PREFIXES):
            raise ValueError(f"key {key!r} does not start with an allowed prefix {ALLOWED_KEY_PREFIXES}")

    def _call(self, tool, args, log_label, timeout=None):
        timeout = timeout or MCP_CALL_TIMEOUT_SECONDS
        action_key = f"{tool}:{uuid4()}"
        start = time.time()
        try:
            result = self._run_coro(
                self._session.call_tool(tool, args, read_timeout_seconds=timeout),
                timeout + 0.5,
            )
            if getattr(result, "is_error", False):
                raise RedisMCPError(_result_error_text(result))
            elapsed_ms = int((time.time() - start) * 1000)
            logger.info("redis_mcp %s action=%s status=ok elapsed_ms=%d", log_label, action_key, elapsed_ms)
            return _result_text(result)
        except RedisMCPError:
            raise
        except Exception as e:
            elapsed_ms = int((time.time() - start) * 1000)
            logger.exception("redis_mcp %s action=%s status=error elapsed_ms=%d", log_label, action_key, elapsed_ms)
            raise RedisMCPError(f"{log_label} failed") from e

    # ---- operations ----
    def rpush(self, key, value, expire=None):
        self._validate_key(key)
        args = {"name": key, "value": value}
        if expire:
            args["expire"] = expire
        self._call("rpush", args, f"rpush key={key}")
        return True

    def lpop(self, key):
        self._validate_key(key)
        text = self._call("lpop", {"name": key}, f"lpop key={key}")
        if text is None or text == f"List '{key}' is empty or does not exist.":
            return None
        return text

    def lrange(self, key, start, stop):
        self._validate_key(key)
        text = self._call("lrange", {"name": key, "start": start, "stop": stop}, f"lrange key={key}")
        if text is None or text == f"List '{key}' is empty or does not exist.":
            return []
        try:
            return json.loads(text)
        except (ValueError, TypeError):
            return [text]

    def llen(self, key):
        self._validate_key(key)
        text = self._call("llen", {"name": key}, f"llen key={key}")
        try:
            return int(text)
        except (TypeError, ValueError):
            return 0

    def get(self, key):
        self._validate_key(key)
        text = self._call("get", {"key": key}, f"get key={key}")
        if text is None or text == f"Key {key} does not exist":
            return None
        return text

    def set(self, key, value, expiration=None):
        self._validate_key(key)
        args = {"key": key, "value": value}
        if expiration:
            args["expiration"] = expiration
        self._call("set", args, f"set key={key}")
        return True


def _result_text(result):
    for block in getattr(result, "content", None) or []:
        text = getattr(block, "text", None)
        if text is not None:
            return text
    structured = getattr(result, "structured_content", None)
    if structured is not None:
        return structured if isinstance(structured, str) else json.dumps(structured)
    return None


def _result_error_text(result):
    parts = [getattr(b, "text", "") for b in (getattr(result, "content", None) or [])]
    return "; ".join(p for p in parts if p) or "Redis MCP tool reported is_error=True"


_client = None
_client_lock = threading.Lock()


def _get_client():
    global _client
    if _client is None:
        with _client_lock:
            if _client is None:
                _client = _RedisMCPClient()
    return _client


def redis_rpush(key, value, expire=None):
    """Push value (a str — callers JSON-encode structured values themselves)
    onto the right (tail) of the list at key. key must start with an allowed
    prefix."""
    return _get_client().rpush(key, value, expire=expire)


def redis_lpop(key):
    """Atomically pop and return the leftmost (head) value of the list at
    key, or None if the list is empty/absent. This is the sole primitive
    slot reservation relies on for lock-free "claim the next item"."""
    return _get_client().lpop(key)


def redis_lrange(key, start, stop):
    """Non-destructive peek: list of values in [start, stop] (inclusive),
    or [] if the list is empty/absent."""
    return _get_client().lrange(key, start, stop)


def redis_llen(key):
    """Current length of the list at key (0 if absent)."""
    return _get_client().llen(key)


def redis_get(key):
    """String value at key, or None if absent."""
    return _get_client().get(key)


def redis_set(key, value, expiration=None):
    """Set key to value (a str), optionally with a TTL in seconds."""
    return _get_client().set(key, value, expiration=expiration)
