"""
Mongo MCP runtime client — the only path from this codebase into MongoDB via
the MongoDB MCP server (mongodb-js/mongodb-mcp-server), scoped to exactly two
operations: mcp_find and mcp_insert. Nothing else is exposed.

Connection model
----------------
The MCP server subprocess is launched with MDB_MCP_CONNECTION_STRING in its
OWN environment only (see _spawn_env below — never the parent's full
environment, never logged). mongodb-mcp-server auto-registers that connection
string under the reserved connection id "preconfigured" at startup, so every
find/insert call below passes connectionId="preconfigured" directly — no
separate connect() handshake is required (confirmed against the installed
mongodb-mcp-server: see common/connectionRegistry.js's PRECONFIGURED_CONNECTION_ID
and tools/mongodb/connect/connect.js's own docstring, which says as much).

Collection allow-list
----------------------
No caller — request payload, patient doc, anything — can name a collection
outside ALLOWED_COLLECTIONS; that is enforced here, not left to the MCP
server or to callers.

Required env vars (.env, os.getenv only — never hardcoded, never logged)
--------------------------------------------------------------------------
MDB_MCP_CONNECTION_STRING   Mongo connection string. Exported only into the
                             MCP server subprocess's environment.
MDB_MCP_SERVER_COMMAND       Launch command for the MCP server (default: npx).
MDB_MCP_SERVER_ARGS          Launch args (default: "-y mongodb-mcp-server").
MDB_MCP_DB_NAME              Database name scoped into every find/insert (default: aircare).
MCP_CALL_TIMEOUT_SECONDS     Bounded per-call timeout, seconds (default: 3).

Construction is lazy (see _get_client): importing this module never spawns
the MCP subprocess, so unit tests can monkeypatch mcp_find/mcp_insert without
ever touching a real Mongo MCP server.
"""

import asyncio
import json
import logging
import os
import threading
import time
from contextlib import AsyncExitStack
from uuid import uuid4

from dotenv import load_dotenv
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client



load_dotenv()  # this project has no other .env loading path — picks up the repo-root .env

logger = logging.getLogger("mongo_mcp_client")

# The winback dashboard collections (config-driven, legacy db_utils.MongoDB
# path) are added here so process_call_status_thread2
# (plugins/project/routes.py) can write the same customer/report/reject
# records through the MCP client instead. Kept alongside the AQI-outbound
# literals rather than in a separate set, since this is still the single
# enforced allow-list callers must go through.
ALLOWED_COLLECTIONS = {
    "patients", "call_logs", "risk_thresholds", "calls", "call_events", "projects",
}

MDB_MCP_DB_NAME = os.getenv("MDB_MCP_DB_NAME", "aircare")
MCP_CALL_TIMEOUT_SECONDS = float(os.getenv("MCP_CALL_TIMEOUT_SECONDS", "3"))
PRECONFIGURED_CONNECTION_ID = "preconfigured"


class MongoMCPError(RuntimeError):
    """A definitive (non-ambiguous) Mongo MCP failure — safe to treat as failed."""


class _MongoMCPClient:
    """One subprocess + one background asyncio loop for the life of the process.

    Route handlers run on FastAPI's own event loop/threads; every call here
    is dispatched onto this client's dedicated loop via _run_coro so the MCP
    stdio session (which is not thread-safe) is only ever touched from one
    thread.
    """

    def __init__(self):
        self._connection_string = os.getenv("MDB_MCP_CONNECTION_STRING")
        if not self._connection_string:
            raise RuntimeError("MDB_MCP_CONNECTION_STRING is not set")

        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()

        self._exit_stack = None
        self._session = None
        self._find_tool = None
        self._insert_tool = None  # name of whichever bulk-insert tool the server exposes
        self._update_tool = None  # optional — see _async_init

        fut = asyncio.run_coroutine_threadsafe(self._async_init(), self._loop)
        fut.result(timeout=30)
        logger.info(
            "MongoMCPClient ready: find_tool=%s insert_tool=%s update_tool=%s db=%s",
            self._find_tool, self._insert_tool, self._update_tool, MDB_MCP_DB_NAME,
        )

    def _run_loop(self):
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    async def _async_init(self):
        command = os.getenv("MDB_MCP_SERVER_COMMAND", "npx")
        args = os.getenv("MDB_MCP_SERVER_ARGS", "-y mongodb-mcp-server").split()
        # get_default_environment() (mcp.client.stdio) only inherits a small,
        # safe allow-list (PATH, HOME, ...) — NOT this process's full
        # environment — so MDB_MCP_CONNECTION_STRING has to be handed to the
        # subprocess explicitly here. It never becomes a CLI arg (so it never
        # shows up in a process listing) and this module never logs it.
        spawn_env = {"MDB_MCP_CONNECTION_STRING": self._connection_string}
        server_params = StdioServerParameters(command=command, args=args, env=spawn_env)

        self._exit_stack = AsyncExitStack()
        read, write = await self._exit_stack.enter_async_context(stdio_client(server_params))
        self._session = await self._exit_stack.enter_async_context(ClientSession(read, write))
        await self._session.initialize()

        tools = (await self._session.list_tools()).tools
        tool_names = {t.name for t in tools}
        logger.info("Mongo MCP discovered tools: %s", sorted(tool_names))

        if "find" not in tool_names:
            raise RuntimeError(f"Required Mongo MCP tool 'find' not found. Discovered: {sorted(tool_names)}")
        self._find_tool = "find"

        for candidate in ("insert-many", "insert-one", "insert"):
            if candidate in tool_names:
                self._insert_tool = candidate
                break
        if not self._insert_tool:
            raise RuntimeError(f"No Mongo MCP insert tool found. Discovered: {sorted(tool_names)}")

        # Optional — not every deployment's mongodb-mcp-server build exposes
        # an update tool, and nothing else here has ever needed one, so its
        # absence must not break find/insert (unlike the two above, this is
        # never added to `required`). update() raises MongoMCPError at call
        # time if it's missing, rather than failing client construction.
        for candidate in ("update-many", "update-one", "update"):
            if candidate in tool_names:
                self._update_tool = candidate
                break

    def _run_coro(self, coro, timeout):
        fut = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return fut.result(timeout=timeout)

    @staticmethod
    def _validate_collection(collection):
        if collection not in ALLOWED_COLLECTIONS:
            raise ValueError(f"collection {collection!r} is not in the allow-list {sorted(ALLOWED_COLLECTIONS)}")

    # ---- find ----
    def find(self, collection, filter_, projection=None, limit=1, sort=None, call_id=None, timeout=None):
        self._validate_collection(collection)
        timeout = timeout or MCP_CALL_TIMEOUT_SECONDS
        action_key = f"{call_id or 'unscoped'}:{collection}:{uuid4()}"
        args = {
            "database": MDB_MCP_DB_NAME,
            "collection": collection,
            "filter": filter_ or {},
            "limit": limit,
            "connectionId": PRECONFIGURED_CONNECTION_ID,
        }
        if projection:
            args["projection"] = projection
        if sort:
            args["sort"] = sort

        start = time.time()
        try:
            result = self._run_coro(
                self._session.call_tool(self._find_tool, args, read_timeout_seconds=timeout),
                timeout + 0.5,
            )
            docs = _extract_documents(result)
            elapsed_ms = int((time.time() - start) * 1000)
            logger.info(
                "mongo_mcp find action=%s collection=%s filter_fields=%s limit=%s status=ok elapsed_ms=%d result_count=%d",
                action_key, collection, sorted((filter_ or {}).keys()), limit, elapsed_ms, len(docs),
            )
            return docs
        except Exception as e:
            elapsed_ms = int((time.time() - start) * 1000)
            logger.exception(
                "mongo_mcp find action=%s collection=%s filter_fields=%s status=error elapsed_ms=%d",
                action_key, collection, sorted((filter_ or {}).keys()), elapsed_ms,
            )
            raise MongoMCPError(f"find failed for collection={collection!r}") from e

    # ---- insert ----
    def insert(self, collection, document, call_id=None, dedupe_filter=None, timeout=None):
        self._validate_collection(collection)
        timeout = timeout or MCP_CALL_TIMEOUT_SECONDS
        action_key = f"{call_id or 'unscoped'}:{collection}:{uuid4()}"
        documents = document if isinstance(document, list) else [document]
        field_names = sorted({k for doc in documents for k in doc.keys()})

        def _do_insert():
            args = {
                "database": MDB_MCP_DB_NAME,
                "collection": collection,
                "connectionId": PRECONFIGURED_CONNECTION_ID,
            }
            if self._insert_tool == "insert-one":
                args["document"] = documents[0]
            else:
                args["documents"] = documents
            return self._run_coro(
                self._session.call_tool(self._insert_tool, args, read_timeout_seconds=timeout),
                timeout + 0.5,
            )

        start = time.time()
        try:
            result = _do_insert()
            ok, inserted_count = _extract_insert_ack(result)
            elapsed_ms = int((time.time() - start) * 1000)
            if not ok:
                logger.error(
                    "mongo_mcp insert action=%s collection=%s fields=%s status=not_acknowledged elapsed_ms=%d",
                    action_key, collection, field_names, elapsed_ms,
                )
                raise MongoMCPError(f"insert into {collection!r} was not acknowledged by the MCP tool")
            logger.info(
                "mongo_mcp insert action=%s collection=%s fields=%s status=ok elapsed_ms=%d inserted_count=%d",
                action_key, collection, field_names, elapsed_ms, inserted_count,
            )
            return True
        except (TimeoutError, asyncio.TimeoutError):
            elapsed_ms = int((time.time() - start) * 1000)
            logger.warning(
                "mongo_mcp insert action=%s collection=%s fields=%s status=timeout elapsed_ms=%d — re-checking before any retry",
                action_key, collection, field_names, elapsed_ms,
            )
            if dedupe_filter:
                existing = self.find(collection, dedupe_filter, limit=1, call_id=call_id, timeout=timeout)
                if existing:
                    logger.info(
                        "mongo_mcp insert action=%s collection=%s status=already_written (found on re-check, not retried)",
                        action_key, collection,
                    )
                    return True
            # Nothing found on re-check (or nothing to dedupe on) — one
            # bounded retry, never a blind/looping retry.
            result = _do_insert()
            ok, inserted_count = _extract_insert_ack(result)
            if not ok:
                raise MongoMCPError(f"insert retry into {collection!r} was not acknowledged by the MCP tool")
            logger.info(
                "mongo_mcp insert action=%s collection=%s status=ok_on_retry inserted_count=%d",
                action_key, collection, inserted_count,
            )
            return True
        except MongoMCPError:
            raise
        except Exception as e:
            elapsed_ms = int((time.time() - start) * 1000)
            logger.exception(
                "mongo_mcp insert action=%s collection=%s fields=%s status=error elapsed_ms=%d",
                action_key, collection, field_names, elapsed_ms,
            )
            raise MongoMCPError(f"insert failed for collection={collection!r}") from e

    # ---- update ----
    def update(self, collection, filter_, update_fields, call_id=None, timeout=None):
        self._validate_collection(collection)
        if not self._update_tool:
            raise MongoMCPError(f"no Mongo MCP update tool available (deployment's server exposes no update-many/update-one/update)")
        timeout = timeout or MCP_CALL_TIMEOUT_SECONDS
        action_key = f"{call_id or 'unscoped'}:{collection}:{uuid4()}"
        args = {
            "database": MDB_MCP_DB_NAME,
            "collection": collection,
            "filter": filter_ or {},
            "update": {"$set": update_fields},
            "connectionId": PRECONFIGURED_CONNECTION_ID,
        }
        start = time.time()
        try:
            result = self._run_coro(
                self._session.call_tool(self._update_tool, args, read_timeout_seconds=timeout),
                timeout + 0.5,
            )
            ok, matched_count = _extract_update_ack(result)
            elapsed_ms = int((time.time() - start) * 1000)
            logger.info(
                "mongo_mcp update action=%s collection=%s status=%s elapsed_ms=%d matched_count=%d",
                action_key, collection, "ok" if ok else "not_matched", elapsed_ms, matched_count,
            )
            return ok
        except MongoMCPError:
            raise
        except Exception as e:
            elapsed_ms = int((time.time() - start) * 1000)
            logger.exception(
                "mongo_mcp update action=%s collection=%s status=error elapsed_ms=%d",
                action_key, collection, elapsed_ms,
            )
            raise MongoMCPError(f"update failed for collection={collection!r}") from e


def _extract_documents(result):
    if getattr(result, "is_error", False):
        raise MongoMCPError(_result_error_text(result))
    structured = getattr(result, "structured_content", None) or {}
    if isinstance(structured, dict) and "documents" in structured:
        return list(structured["documents"] or [])
    # Defensive fallback for a transport that drops structured_content: the
    # find tool's second content block (only present when there are results)
    # is `JSON.stringify(serializedDocuments)` — see find.js's generateMessage/execute.
    for block in getattr(result, "content", None) or []:
        text = getattr(block, "text", None)
        if not text:
            continue
        try:
            parsed = json.loads(text)
        except (ValueError, TypeError):
            continue
        if isinstance(parsed, list):
            return parsed
    return []


def _extract_insert_ack(result):
    """(ok, inserted_count). ok is only True when the tool's own structured
    result reports at least one inserted document — never inferred merely
    from "the call didn't raise"."""
    if getattr(result, "is_error", False):
        raise MongoMCPError(_result_error_text(result))
    structured = getattr(result, "structured_content", None) or {}
    inserted_count = structured.get("insertedCount")
    if inserted_count is None:
        return False, 0
    return inserted_count > 0, inserted_count


def _extract_update_ack(result):
    """(ok, matched_count). ok is True as soon as a document matched the
    filter — even if the new value equals the old one and nothing actually
    changed (modifiedCount==0), that's still a successful, found-and-set
    update, not a failure."""
    if getattr(result, "is_error", False):
        raise MongoMCPError(_result_error_text(result))
    structured = getattr(result, "structured_content", None) or {}
    matched_count = structured.get("matchedCount")
    if matched_count is None:
        return False, 0
    return matched_count > 0, matched_count


def _result_error_text(result):
    parts = [getattr(b, "text", "") for b in (getattr(result, "content", None) or [])]
    return "; ".join(p for p in parts if p) or "Mongo MCP tool reported is_error=True"


_client = None
_client_lock = threading.Lock()


def _get_client():
    global _client
    if _client is None:
        with _client_lock:
            if _client is None:
                _client = _MongoMCPClient()
    return _client


def mcp_find(collection, filter, projection=None, limit=1, sort=None, call_id=None, timeout=None):
    """Find in `collection` (must be in ALLOWED_COLLECTIONS). Returns a list of
    plain dicts (possibly empty), never raises on "no results" — only on a
    genuine MCP/transport failure (MongoMCPError) or a disallowed collection
    (ValueError)."""
    return _get_client().find(collection, filter, projection=projection, limit=limit, sort=sort, call_id=call_id, timeout=timeout)


def mcp_insert(collection, document, call_id=None, dedupe_filter=None, timeout=None):
    """Insert one document (dict) or many (list of dicts) into `collection`
    (must be in ALLOWED_COLLECTIONS). Returns True only once the MCP tool's
    own response confirms at least one document was actually inserted;
    raises MongoMCPError otherwise. Pass dedupe_filter (e.g. {"call_id": ...})
    so an ambiguous timeout re-checks by that business key instead of
    blindly retrying the insert."""
    return _get_client().insert(collection, document, call_id=call_id, dedupe_filter=dedupe_filter, timeout=timeout)


def mcp_update(collection, filter, update_fields, call_id=None, timeout=None):
    """$set `update_fields` onto every document in `collection` (must be in
    ALLOWED_COLLECTIONS) matching `filter`. Returns True only once the MCP
    tool's own response confirms at least one document matched; raises
    MongoMCPError otherwise, or if this deployment's Mongo MCP server exposes
    no update tool at all (update is optional — see _MongoMCPClient._async_init)."""
    return _get_client().update(collection, filter, update_fields, call_id=call_id, timeout=timeout)
