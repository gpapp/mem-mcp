"""
common.py – Shared configuration, logging, DB clients, and common helpers.
"""

from typing import Any, List, Optional
import os
import json
import re
import uuid
import time
import socket
import secrets
import logging
import base64
import httpx
import numpy as np
import asyncio
import re
from datetime import datetime
from pathlib import Path
from logging.handlers import RotatingFileHandler

import status_monitor


def _load_env_file() -> None:
    """Load the repository .env for direct local server launches.

    Explicit process environment variables win over values in the file, which
    keeps Docker Compose and production deployments authoritative.
    """
    candidates = [Path.cwd() / ".env", Path(__file__).resolve().parent.parent / ".env"]
    env_path = next((path for path in candidates if path.is_file()), None)
    if env_path is None:
        return

    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            os.environ.setdefault(key, value)


_load_env_file()

LOG_LEVEL_NAME = os.getenv("LOG_LEVEL") or "INFO"
LOG_LEVEL = getattr(logging, LOG_LEVEL_NAME.upper(), logging.INFO)
LOG_DIR = os.getenv("LOG_DIR") or str(Path(__file__).resolve().parent.parent / "logs")
os.makedirs(LOG_DIR, exist_ok=True)

# Savepoints live beside the logs so a single volume mount covers both. In the
# container LOG_DIR is /app/logs, which puts backups at /app/backup; locally it
# lands next to the repo's logs/ directory.
BACKUP_DIR = os.getenv("MEM_BACKUP_DIR") or os.path.join(os.path.dirname(LOG_DIR), "backup")
os.makedirs(BACKUP_DIR, exist_ok=True)


# ---------------------------------------------------------------------------
# Maintenance lock
#
# Reclassification and backup/restore both rewrite large parts of the graph. Run
# them concurrently and the two jobs interleave their writes, so a restore can
# resurrect links a reclassify just deleted (or the reverse). One owner per user
# at a time; the holder releases in a finally block.
# ---------------------------------------------------------------------------
_MAINTENANCE_OWNERS: dict = {}


def claim_maintenance(user_id: str, operation: str) -> bool:
    """Take the maintenance lock for a user. False if another job holds it."""
    if _MAINTENANCE_OWNERS.get(user_id):
        return False
    _MAINTENANCE_OWNERS[user_id] = operation
    return True


def release_maintenance(user_id: str) -> None:
    _MAINTENANCE_OWNERS.pop(user_id, None)


def current_maintenance(user_id: str) -> Optional[str]:
    return _MAINTENANCE_OWNERS.get(user_id)


def active_maintenance() -> dict:
    """Every user currently running a maintenance job, mapped to the operation.

    A scheduled backup is vault-wide but the lock is per-user, so it cannot
    simply take the lock: it has to check that no user is mid-reclassify.
    """
    return dict(_MAINTENANCE_OWNERS)



class _StripAnsiFilter(logging.Filter):
    _ANSI_ESCAPE = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = self._ANSI_ESCAPE.sub("", record.msg)
        if record.args:
            record.args = tuple(
                self._ANSI_ESCAPE.sub("", arg) if isinstance(arg, str) else arg
                for arg in record.args
            )
        return True

# Session secret – must be set via environment (e.g., Docker). No fallback.
import sessions

SESSION_SECRET = os.getenv("MEM_SESSION_SECRET")
# SESSION_SECRET defined earlier with strict environment check
if not SESSION_SECRET:
    raise RuntimeError("MEM_SESSION_SECRET is not set in environment. Please set it to a stable secret.")

from qdrant_client import AsyncQdrantClient
from qdrant_client.models import (
    Distance, VectorParams
)
from neo4j import GraphDatabase

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
root_logger = logging.getLogger()
root_logger.setLevel(LOG_LEVEL)
if not root_logger.handlers:
    logging.basicConfig(level=LOG_LEVEL)
for handler in root_logger.handlers:
    handler.setLevel(LOG_LEVEL)

if not any(isinstance(handler, RotatingFileHandler) and handler.name == "memory-vault-file"
           for handler in root_logger.handlers):
    file_handler = RotatingFileHandler(
        os.path.join(LOG_DIR, "memory-vault.log"),
        maxBytes=10 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.name = "memory-vault-file"
    file_handler.setLevel(LOG_LEVEL)
    file_handler.addFilter(_StripAnsiFilter())
    file_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    root_logger.addHandler(file_handler)

logger = logging.getLogger("memory-vault")
logger.setLevel(LOG_LEVEL)
logging.getLogger("mcp").setLevel(LOG_LEVEL)

# Per-query search ranking audit log. Deliberately independent of LOG_LEVEL:
# production runs at WARNING, which would otherwise silence the only data that
# shows how search actually ranked a query after a deploy.
search_logger = logging.getLogger("mem.search")
search_logger.setLevel(logging.INFO)
search_logger.propagate = False
if not any(getattr(h, "name", None) == "search-stats-file" for h in search_logger.handlers):
    _search_handler = RotatingFileHandler(
        os.path.join(LOG_DIR, "search_stats.log"),
        maxBytes=10 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    _search_handler.name = "search-stats-file"
    _search_handler.setLevel(logging.INFO)
    _search_handler.addFilter(_StripAnsiFilter())
    _search_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    search_logger.addHandler(_search_handler)


def log_search_stats(**fields) -> None:
    """Record one line of per-query search ranking data to search_stats.log."""
    search_logger.info("search_stats %s", json.dumps(fields, default=str))


class _BenignScopeNotificationFilter(logging.Filter):
    """Drop expected Neo4j UNRECOGNIZED notifications for optional scope schema.

    The Client/Context labels and FOR_CLIENT/IN_CONTEXT relationship types only
    materialize once the first such node/relationship is created (via migration
    or client-scoped writes). OPTIONAL MATCH over not-yet-existing schema is
    valid and simply matches nothing — the 01N50/01N51 warnings are noise until
    then. The 01G11 null-elimination notice from collect() over the optional
    scope match is likewise expected. Gated on both the status code and our
    identifiers so genuine schema warnings for anything else still surface.
    """
    # Both backticked (notification descriptions) and colon-prefixed (raw query
    # text, e.g. "(c:Client" / "[:HAS_CONTEXT]") forms are matched.
    _SUPPRESSED = ("`Context`", "`Client`", "`IN_CONTEXT`", "`FOR_CLIENT`", "`HAS_CONTEXT`",
                   ":Client", ":Context", ":IN_CONTEXT", ":FOR_CLIENT", ":HAS_CONTEXT")

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:
            return True
        if "01N50" in msg or "01N51" in msg or "01G11" in msg:
            return not any(s in msg for s in self._SUPPRESSED)
        return True


logging.getLogger("neo4j.notifications").addFilter(_BenignScopeNotificationFilter())

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
QDRANT_URL     = os.getenv("MEM_QDRANT_URL",      "http://qdrant:6333")
NEO4J_URL      = os.getenv("MEM_NEO4J_URL",       "bolt://neo4j:7687")
NEO4J_USER     = os.getenv("MEM_NEO4J_USER",      "neo4j")
NEO4J_PASS     = os.getenv("MEM_NEO4J_PASSWORD",  "password")
OLLAMA_URL      = os.getenv("MEM_LLM_URL",         os.getenv("MEM_EMBEDDER_URL", "http://ollama:11434"))
EMBED_MODEL     = os.getenv("MEM_EMBEDDER_MODEL",  "nomic-embed-text")
LLM_QUERY_MODEL = os.getenv("LLM_QUERY_MODEL") or "qwen3.5:0.8b"
# Model used for deliberate merge-draft generation. Keep search/classification
# on the smaller query model unless an operator explicitly changes them.
MERGE_MODEL = os.getenv("MEM_MERGE_MODEL") or "gemma4:e2b"
# Model used for server-side scope classification (client/context backfill).
# Override with MEM_SCOPE_MODEL if a more capable model is available in Ollama.
SCOPE_MODEL = os.getenv("MEM_SCOPE_MODEL") or LLM_QUERY_MODEL
# Model for EXTRACTION roles: diary keywords, people names, search rewriting.
# Split out because extraction and judgement want different models, and one
# knob cannot be right for both. Extraction is high-volume and mechanical, so it
# wants the fastest model that will not invent terms; judgement is low-volume
# and consequential, so it wants the model that does not guess. Measured on this
# host: granite3.3:2b beat nemotron-3-nano:4b on keyword precision (99.0% vs
# 79.7%), invented keywords (0 vs 5) and people-name F1 (0.938 vs 0.920), while
# being 2.3-4.2x faster. Nemotron kept judgement roles: it answered `merge` on a
# true duplicate where granite answered `review`, and it is the only one of the
# two to return zero null scope verdicts -- a null is stamped with
# scopeCheckedSig and never revisited, so it is permanent, not a retry.
# Defaults to LLM_QUERY_MODEL, so an operator who sets nothing changes nothing.
EXTRACT_MODEL = os.getenv("MEM_EXTRACT_MODEL") or LLM_QUERY_MODEL
# Set MEM_SCOPE_BACKFILL=0 to skip the LLM scope backfill pass at startup.
SCOPE_BACKFILL_ENABLED = os.getenv("MEM_SCOPE_BACKFILL", "1") == "1"
SCOPE_BACKFILL_CONCURRENCY = int(os.getenv("MEM_SCOPE_CONCURRENCY", "3"))
HTTP_TIMEOUT    = float(os.getenv("MEM_HTTP_TIMEOUT", "300.0"))
# Read timeout for a single Ollama /api/chat call. This is deliberately its own
# knob rather than reusing HTTP_TIMEOUT: a chat is the one call that can take
# minutes of wall clock, because it is CPU inference on a small model with no
# GPU, and it scales with the prompt. The old hardcoded 60s cut reclassification
# off mid-window -- one window's LLM call was measured taking 46s for a 4420-char
# prompt, so a merely slow host turned into a 503 with nothing logged. Connect
# stays short so a genuinely dead Ollama fails fast instead of burning 5 minutes.
LLM_CONNECT_TIMEOUT = float(os.getenv("MEM_LLM_CONNECT_TIMEOUT", "10.0"))
LLM_TIMEOUT    = float(os.getenv("MEM_LLM_TIMEOUT", "300.0"))
# The search-rewrite call sits between the user and their results, so it keeps a
# short budget: a rewrite that takes two minutes is worse than no rewrite, because
# the query is already good enough to search with. The background passes
# (classification, extraction, merge drafts) get the full LLM_TIMEOUT instead.
SEARCH_LLM_TIMEOUT = float(os.getenv("MEM_SEARCH_LLM_TIMEOUT", "45.0"))
# How much of each prompt and answer is written to the chat log line. The whole
# request and the whole answer used to be unrecoverable -- only the character
# *counts* were logged, so a mis-scoped or hallucinating answer could not be
# inspected after the fact without reproducing it. Set to 0 to log sizes only;
# note that this writes meeting content and entry text to the log file, which is
# worth knowing if the vault holds anything you would not want at rest there.
LLM_LOG_CHARS = max(0, int(os.getenv("MEM_LLM_LOG_CHARS", "1000")))
BASE_URL       = os.getenv("BASE_URL",            "").rstrip("/")

COLLECTION_NAME  = "ea_memories"
DIARY_COLLECTION = "ea_diary"


SESSION_MAX_AGE = 30 * 24 * 60 * 60  # 30 days in seconds

# ---------------------------------------------------------------------------
# Global DB client references (lazily populated)
# ---------------------------------------------------------------------------
_qdrant: Optional[AsyncQdrantClient] = None
_neo4j_driver = None
_db_initialized = False
_db_lock = asyncio.Lock()

# Global event queue for database changes
db_subscribers: List[asyncio.Queue] = []

async def publish_db_event(user_id: str, event_type: str, payload: Optional[dict] = None):
    """Publishes a database change event to the global queue."""
    event = {
        "user_id": user_id,
        "event_type": event_type,
        "payload": payload or {},
        "timestamp": datetime.now().isoformat()
    }
    for queue in db_subscribers:
        await queue.put(event)

async def get_qdrant() -> AsyncQdrantClient:
    global _qdrant, _db_initialized
    async with _db_lock:
        if _qdrant is None:
                    _qdrant = AsyncQdrantClient(url=QDRANT_URL, check_compatibility=False)
        
        if not _db_initialized:
            if wait_for_service(QDRANT_URL, "Qdrant"):
                try:
                    cols = await _qdrant.get_collections()
                    existing = [c.name for c in cols.collections]
                    if COLLECTION_NAME not in existing:
                        await _qdrant.create_collection(
                            collection_name=COLLECTION_NAME,
                            vectors_config=VectorParams(size=768, distance=Distance.COSINE),
                        )
                    if DIARY_COLLECTION not in existing:
                        await _qdrant.create_collection(
                            collection_name=DIARY_COLLECTION,
                            vectors_config=VectorParams(size=768, distance=Distance.COSINE),
                        )
                    _db_initialized = True
                except Exception as e:
                    logger.error(f"Qdrant init error: {e}")
    return _qdrant

def get_neo4j():
    global _neo4j_driver
    if _neo4j_driver is None:
        if wait_for_service(NEO4J_URL, "Neo4j"):
            try:
                _neo4j_driver = GraphDatabase.driver(NEO4J_URL, auth=(NEO4J_USER, NEO4J_PASS))
                with _neo4j_driver.session() as s:
                    s.run("CREATE INDEX fact_user_id_index IF NOT EXISTS FOR (f:Fact) ON (f.userId)")
                    s.run("CREATE INDEX fact_category_index IF NOT EXISTS FOR (f:Fact) ON (f.category)")
                    s.run("CREATE INDEX diary_date_index IF NOT EXISTS FOR (d:DiaryEntry) ON (d.date)")
                    s.run("OPTIONAL MATCH (a)-[:MENTIONS]->(b) RETURN 1 LIMIT 0")
            except Exception as e:
                logger.error(f"Neo4j init error: {e}")
                if _neo4j_driver is not None:
                    _neo4j_driver.close()
                _neo4j_driver = None
    return _neo4j_driver

# ---------------------------------------------------------------------------
# Service readiness
# ---------------------------------------------------------------------------
def _parse_url(url: str):
    clean = url.replace("http://", "").replace("bolt://", "").split("/")[0]
    if ":" in clean:
        host, port = clean.split(":", 1)
        return host, int(port)
    return clean, 80

def wait_for_service(url: str, label: str, max_retries: int = 5) -> bool:
    host, port = _parse_url(url)
    for _ in range(max_retries):
        try:
            with socket.create_connection((host, port), timeout=2):
                logger.info(f"{label} is ready at {host}:{port}")
                return True
        except Exception:
            time.sleep(2)
    logger.warning(f"{label} not reachable after {max_retries} retries")
    return False


def _ollama_model_matches(installed, wanted: str) -> bool:
    """Is ``wanted`` already present in Ollama's reported model names?

    Ollama reports a tagless model as ``name:latest`` and an untagged one as
    ``name:any``, while the configuration usually just says ``name``. A plain
    set membership test therefore never matches and the model is re-pulled on
    every boot -- 23 unnecessary downloads of a 274MB embedder in one week.
    """
    wanted = (wanted or "").strip()
    if not wanted:
        return False
    if wanted in installed:
        return True
    # A configured name that already carries a tag must match exactly, or via
    # the implicit :latest. Only a tagless name may also satisfy :any.
    base, sep, tag = wanted.rpartition(":")
    if not sep:
        base, tag = wanted, ""
    for name in installed:
        name_base, _, name_tag = name.rpartition(":")
        if not _:
            continue
        if name_base != base:
            continue
        if not tag or name_tag in (tag, "latest") or (not tag and name_tag == "any"):
            return True
    return False


async def ensure_ollama_models() -> None:
    """Ensure every configured Ollama model is available before startup work."""
    models = list(dict.fromkeys((EMBED_MODEL, LLM_QUERY_MODEL, EXTRACT_MODEL,
                                 SCOPE_MODEL, MERGE_MODEL)))
    if not wait_for_service(OLLAMA_URL, "Ollama"):
        raise RuntimeError(f"Ollama is not reachable at {OLLAMA_URL}")

    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
        logger.warning(f"Ollama request: GET {OLLAMA_URL}/api/tags")
        response = await client.get(f"{OLLAMA_URL}/api/tags")
        logger.warning(
            f"Ollama response: GET /api/tags status={response.status_code} body={response.text}"
        )
        response.raise_for_status()
        installed = {
            model.get("name")
            for model in response.json().get("models", [])
            if model.get("name")
        }

        for model in models:
            if _ollama_model_matches(installed, model):
                logger.warning(f"Ollama result: model ready: {model}")
                continue

            logger.warning(f"Ollama model missing; downloading: {model}")
            pull_request = {"name": model, "stream": True}
            logger.warning(f"Ollama request: POST {OLLAMA_URL}/api/pull body={pull_request}")
            async with client.stream(
                "POST",
                f"{OLLAMA_URL}/api/pull",
                json=pull_request,
            ) as pull_response:
                pull_response.raise_for_status()
                last_status = ""
                async for line in pull_response.aiter_lines():
                    if not line:
                        continue
                    update = json.loads(line)
                    status = update.get("status", "")
                    if status and status != last_status:
                        logger.warning(f"Ollama response: POST /api/pull model={model} update={update}")
                        last_status = status
                    if update.get("error"):
                        raise RuntimeError(f"Ollama failed to pull {model}: {update['error']}")

            logger.warning(f"Ollama result: model download complete: {model}")


# ---------------------------------------------------------------------------
# Server status
#
# A snapshot of Ollama's residency, for the status widget in the corner of the
# Memories/Diary pages and the model panel on the Setup page. It is polled on a
# slow interval and pushed over SSE only when the fingerprint changes, so this
# is deliberately not on the request path and carries its own short timeout: a
# status probe must never be the reason a search is slow. The pure half of the
# snapshot lives in status_monitor.py so it can be tested without httpx.
# ---------------------------------------------------------------------------
STATUS_POLL_SECONDS = max(2.0, float(os.getenv("MEM_STATUS_POLL_SECONDS", "10")))
# Long enough for Ollama to answer on a loaded GPU, short enough that a wedged
# service shows up as "down" rather than as a widget frozen on the last answer.
STATUS_HTTP_TIMEOUT = max(1.0, float(os.getenv("MEM_STATUS_HTTP_TIMEOUT", "8")))


def configured_model_roles() -> list:
    """The role -> model map the status widget labels models with."""
    return status_monitor.model_roles(
        embedder=EMBED_MODEL,
        query=LLM_QUERY_MODEL,
        extract=EXTRACT_MODEL,
        scope=SCOPE_MODEL,
        merge=MERGE_MODEL,
    )


async def fetch_ollama_status() -> dict:
    """One status snapshot. Never raises: a dead service is a snapshot, not an exception.

    A failed endpoint is a *warning*, not an error, so the widget can show the
    service alive but degraded. Only a total failure sets ``error``, which is
    the only thing that turns the indicator red — the alternative is a moved
    route painting the whole service as down.
    """
    roles = configured_model_roles()
    warnings: list = []
    version = tags = ps = None

    try:
        async with httpx.AsyncClient(timeout=STATUS_HTTP_TIMEOUT) as client:
            bodies = {}
            for label, endpoint in (("version", "/api/version"),
                                    ("ps", "/api/ps"),
                                    ("tags", "/api/tags")):
                response = await client.get(f"{OLLAMA_URL}{endpoint}")
                if response.status_code != 200:
                    warnings.append(f"{endpoint} returned HTTP {response.status_code}: "
                                    f"{_ollama_detail(response)}")
                    continue
                try:
                    bodies[label] = response.json()
                except Exception:
                    warnings.append(f"{endpoint} returned a body that is not JSON")
            version, ps, tags = bodies.get("version"), bodies.get("ps"), bodies.get("tags")
    except Exception as exc:
        return status_monitor.build_status(
            roles=roles, url=OLLAMA_URL, maintenance=active_maintenance(),
            error=f"{type(exc).__name__}: {exc}",
        )

    return status_monitor.build_status(
        roles=roles, url=OLLAMA_URL, version=version, tags=tags, ps=ps,
        warnings=warnings, maintenance=active_maintenance(),
    )


async def unload_ollama_model(model: str) -> None:
    """Evict a resident model from VRAM/RAM.

    ``keep_alive: 0`` is Ollama's documented unload, and it is the only
    supported way to do this: there is no DELETE. A model that is not resident
    answers 200 and does nothing, which is why the caller validates the name
    against the live snapshot first — a typo here would otherwise be a silent
    no-op behind a 200.
    """
    body = {"model": model, "keep_alive": 0}
    logger.warning(f"Ollama request: POST {OLLAMA_URL}/api/generate body={body}")
    async with httpx.AsyncClient(timeout=STATUS_HTTP_TIMEOUT) as client:
        try:
            response = await client.post(f"{OLLAMA_URL}/api/generate", json=body)
        except Exception as exc:
            raise RuntimeError(
                f"Could not reach Ollama at {OLLAMA_URL} to unload {model}: "
                f"{type(exc).__name__}: {exc}"
            ) from exc
        if response.status_code != 200:
            detail = _ollama_detail(response)
            logger.warning(
                f"Ollama response: POST /api/generate unload status={response.status_code} body={response.text}"
            )
            raise RuntimeError(
                f"Ollama refused to unload {model} (HTTP {response.status_code}): {detail}"
            )
    logger.warning(f"Ollama result: model unloaded: {model}")


def clean_extracted_people_names(names: list) -> list[str]:
    """Normalize extracted names and remove speaker placeholder labels."""
    cleaned = []
    seen = set()
    for raw_name in names:
        name = re.sub(r"\s+", " ", str(raw_name)).strip()
        if not name or re.fullmatch(r"SPEAKER\s*#?\s*\d+", name, re.IGNORECASE):
            continue
        key = name.casefold()
        if key not in seen:
            seen.add(key)
            cleaned.append(name)
    return cleaned

# ---------------------------------------------------------------------------
# Embedding
# ---------------------------------------------------------------------------
_EMBEDDING_CACHE: dict = {}
EMBED_CACHE_MAX = 2048
EMBED_RETRIES = max(0, int(os.getenv("MEM_EMBED_RETRIES", "2")))
EMBED_RETRY_BACKOFF = float(os.getenv("MEM_EMBED_RETRY_BACKOFF", "1.5"))
# Character budget for one embedding. Ollama serves embedding models with a
# smaller window than the model advertises (num_ctx defaults to 2048-4096
# regardless of the model's real 8192), so a long fact or diary entry can blow
# the limit while looking modest on screen. Measured against the production
# embedder: 11189 chars was refused as too long, 6000 accepted — a window of
# about 2048 tokens, so ~2000 tokens of English is what actually fits. 8000
# chars clears it with headroom. The old 12000 default assumed the upper end of
# that 2048-4096 range and was over budget for this model, which the shrink
# ladder absorbed, so nothing broke — but every long record paid a rejected
# request before succeeding. Raise it if your model genuinely has room, lower it
# if you see 500s in the log.
EMBED_MAX_CHARS = max(500, int(os.getenv("MEM_EMBED_MAX_CHARS", "8000")))
# Statuses worth a second attempt. A 5xx from Ollama is usually a cold model
# load or two requests racing to load the same model, both of which clear.
# 4xx is not retried: a bad model name or a missing route will not fix itself.
_EMBED_RETRY_STATUS = frozenset({408, 409, 429, 500, 502, 503, 504})
# Deterministic failures dressed as a 500. The same bytes will fail the same way
# forever, so these are never retried on the same input — the remedy is a
# smaller input, not another attempt.
_EMBED_INPUT_ERRORS = (
    "exceeds the context length",
    "context length",
    "input is too long",
    "maximum context",
)


def _ollama_detail(resp) -> str:
    """Pull the reason out of an Ollama error body.

    Ollama answers 500 with {"error": "..."} and that string is the only thing
    that distinguishes "model not pulled" from "out of memory" from "route
    removed". Without it the traceback shows a status code and nothing else.
    """
    text = (getattr(resp, "text", "") or "").strip()
    if not text:
        return "(empty response body)"
    try:
        payload = resp.json()
    except ValueError:
        return text[:500]
    if isinstance(payload, dict) and payload.get("error"):
        return str(payload["error"])[:500]
    return text[:500]


async def _embed_once(client, endpoint: str, body: dict) -> tuple:
    """One embed call. Returns (vector, retryable, detail)."""
    resp = await client.post(f"{OLLAMA_URL}{endpoint}", json=body)
    if resp.is_error:
        return None, resp.status_code in _EMBED_RETRY_STATUS, f"HTTP {resp.status_code}: {_ollama_detail(resp)}"
    try:
        payload = resp.json()
    except ValueError:
        return None, False, f"non-JSON response: {(resp.text or '')[:200]}"
    # /api/embed answers {"embeddings": [[...]]}; /api/embeddings answers
    # {"embedding": [...]}. Accept either so a server-side switch is invisible.
    vectors = payload.get("embeddings")
    if isinstance(vectors, list) and vectors and isinstance(vectors[0], list):
        return vectors[0], False, ""
    single = payload.get("embedding")
    if isinstance(single, list) and single:
        return single, False, ""
    return None, False, f"no embedding in response (keys: {sorted(payload)[:6]})"


def _is_input_too_long(detail: str) -> bool:
    """True when Ollama rejected the text itself, not the request."""
    lowered = (detail or "").lower()
    return any(hint in lowered for hint in _EMBED_INPUT_ERRORS)


def _truncate_for_embed(text: str) -> str:
    """Cut to the char budget, keeping the head and tail.

    Head-only truncation is the usual choice, but a long transcription puts the
    subject at the top and the conclusions at the bottom, and the tail is what a
    search query is most likely to match. Both ends are kept.
    """
    if len(text) <= EMBED_MAX_CHARS:
        return text
    # The separator is part of the budget, not an extra on top of it: the point
    # of a ceiling is that the result is guaranteed to fit.
    marker = "\n...\n"
    room = EMBED_MAX_CHARS - len(marker)
    head = int(room * 0.75)
    return f"{text[:head]}{marker}{text[-(room - head):]}"


async def get_embedding(text: str) -> List[float]:
    """Embed text via Ollama, memoizing results for the process lifetime.

    Embeddings are deterministic per (model, text), and a single search fans out
    to one Ollama round trip per query variant. The cache collapses repeats across
    variants, repeats within a request, and repeat searches by the agent.

    The legacy /api/embeddings route is tried first and /api/embed second: the
    Ollama image is unpinned, and the older route is the one that goes away. A
    failure that is plausibly transient is retried before it is surfaced.

    Text longer than ``EMBED_MAX_CHARS`` is truncated to fit the embedder's
    context window, and if Ollama still reports the input as too long the text
    is halved and tried again. Ollama answers that case with a 500, so without
    this an oversized fact would be re-sent five times and then fail outright.

    Logging policy, since embedding is the high-volume path (one call per query
    variant per search, plus one per write) and production runs at
    LOG_LEVEL=WARNING:

      * per-attempt failures and retries — DEBUG. A degraded Ollama must not emit
        a WARNING per call and bury the chat traffic that level exists for.
      * input too long — WARNING. This is rare and it means the stored vector is
        a lossy summary, which the operator needs to know about.
      * embedding failed — ERROR, once, carrying Ollama's own error body and the
        ``ollama pull`` fix. This is the only failure signal for this path.
    """
    key = (EMBED_MODEL, text)
    cached = _EMBEDDING_CACHE.get(key)
    if cached is not None:
        return cached

    if len(text) > EMBED_MAX_CHARS:
        logger.warning(
            f"Embedding {EMBED_MODEL}: input is {len(text)} chars, over the "
            f"{EMBED_MAX_CHARS} budget — truncating (head and tail kept)"
        )
    work = _truncate_for_embed(text)

    endpoints = (
        ("/api/embeddings", {"model": EMBED_MODEL, "prompt": None}),
        ("/api/embed", {"model": EMBED_MODEL, "input": None}),
    )
    last_detail = ""
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
        for endpoint, body in endpoints:
            field = "prompt" if "prompt" in body else "input"
            attempt = 0
            while True:
                body[field] = work
                if attempt:
                    delay = EMBED_RETRY_BACKOFF * attempt
                    logger.debug(
                        f"Ollama embed retry {attempt}/{EMBED_RETRIES} on {endpoint} "
                        f"in {delay:.1f}s — {last_detail}"
                    )
                    await asyncio.sleep(delay)
                try:
                    vector, retryable, detail = await _embed_once(client, endpoint, body)
                except httpx.HTTPError as exc:
                    last_detail = f"{type(exc).__name__}: {exc}"
                    logger.debug(f"Ollama embed transport error on {endpoint}: {last_detail}")
                    attempt += 1
                    if attempt > EMBED_RETRIES:
                        break
                    continue
                if vector is not None:
                    logger.debug(f"Ollama result: embedding model={EMBED_MODEL} dimensions={len(vector)}")
                    if len(_EMBEDDING_CACHE) >= EMBED_CACHE_MAX:
                        _EMBEDDING_CACHE.pop(next(iter(_EMBEDDING_CACHE)))
                    _EMBEDDING_CACHE[key] = vector
                    return vector
                last_detail = detail
                if _is_input_too_long(detail):
                    # Deterministic. Re-sending the same bytes cannot help, and
                    # the budget is only an estimate of the real window — halve
                    # and retry immediately, without spending a backoff.
                    shrunk = work[:len(work) // 2]
                    if len(shrunk) < 200 or shrunk == work:
                        break  # already tiny; something else is wrong
                    logger.warning(
                        f"Ollama embed input too long on {endpoint} "
                        f"({len(work)} chars) — retrying with {len(shrunk)}"
                    )
                    work = shrunk
                    continue
                logger.debug(
                    f"Ollama embed failed on {endpoint} model={EMBED_MODEL} — {detail}"
                )
                if not retryable:
                    break  # this route will not do better; try the other one
                attempt += 1
                if attempt > EMBED_RETRIES:
                    break

    # The one place a failed embed is reported. Every attempt above is DEBUG so a
    # degraded Ollama cannot flood the log; the reason is carried forward and
    # surfaced once, here, with the fix.
    reason = last_detail or "no detail from Ollama"
    logger.error(
        f"Ollama embed failed for model {EMBED_MODEL!r} on both routes: {reason}. "
        f"If the model is missing, run: docker exec ollama ollama pull {EMBED_MODEL}"
    )
    raise RuntimeError(
        f"Embedding failed for model {EMBED_MODEL!r}: {reason}. "
        f"Confirm the Ollama container is healthy and the model is present "
        f"(docker exec ollama ollama pull {EMBED_MODEL})."
    )


def _llm_excerpt(text: str, limit: int = 0) -> str:
    """Shorten text for a single log line, and flatten it to stay one line.

    Prompts and model answers are both multi-line by nature, and a raw newline in
    a RotatingFileHandler record makes one logical event span several lines — at
    which point `grep` reports a truncated fragment as if it were the whole
    message, and the line-oriented tooling reads timestamps that are not there.
    So the excerpt escapes the whitespace it contains rather than emitting it.

    The elision marker carries the *total* length, because the whole reason for
    looking at this is a call that behaved unexpectedly and the first question
    is always "how much was there that I cannot see".
    """
    budget = limit or LLM_LOG_CHARS
    body = text if isinstance(text, str) else str(text)
    if budget <= 0:
        # Content logging turned off. Not "unlimited" -- an operator disabling
        # this wants the sizes back, not every prompt written to disk forever.
        return ""
    if len(body) <= budget:
        excerpt = body
    else:
        excerpt = body[:budget] + f"…+{len(body) - budget} more chars"
    return (excerpt.replace("\\", "\\\\")
                  .replace("\r\n", "\\n")
                  .replace("\n", "\\n")
                  .replace("\r", "\\r")
                  .replace("\t", "\\t"))


async def get_llm_response(prompt: str, system: str = "", model: str = "",
                           num_predict: int = 0, timeout: float = 0.0) -> str:
    """Call Ollama /api/chat and return the assistant's text response.

    Uses LLM_QUERY_MODEL (default: qwen3.5:0.8b) unless overridden by `model`.
    Read timeout is LLM_TIMEOUT (MEM_LLM_TIMEOUT, default 300 s); pass `timeout`
    to shorten it for a call that sits in front of a user waiting on it.
    Strips <think>…</think> blocks produced by reasoning models (e.g. Qwen3).
    Pass num_predict > 0 to cap/guarantee the output token budget.

    Every failure mode is logged with the model, the prompt size and the elapsed
    time, and the ones that mean "no answer is possible" raise RuntimeError
    naming the remedy. A bare httpx exception propagating out of here is what
    made a slow host look like a dead service: the WARNING request line was
    written, the response line never was, and nothing in between explained the
    gap. An *empty* answer is logged but still returned, because callers have
    deliberate fallbacks for it.
    """
    resolved_model = model or LLM_QUERY_MODEL
    messages: list = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})
    options: dict = {"temperature": 0.0}
    if num_predict > 0:
        options["num_predict"] = num_predict
    request_body = {
        "model": resolved_model,
        "messages": messages,
        "stream": False,
        "think": False,
        "options": options,
    }
    budget = timeout or LLM_TIMEOUT
    url = f"{OLLAMA_URL}/api/chat"
    logger.warning(
        f"Ollama request: POST {OLLAMA_URL}/api/chat model={resolved_model} "
        f"prompt_chars={len(prompt)} system_chars={len(system)} timeout_s={budget:g} "
        f"system={_llm_excerpt(system)} prompt={_llm_excerpt(prompt)}"
    )
    started = time.monotonic()
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(budget, connect=LLM_CONNECT_TIMEOUT)
        ) as client:
            resp = await client.post(url, json=request_body)
    except httpx.TimeoutException as exc:
        elapsed = time.monotonic() - started
        logger.error(
            f"LLM timeout after {elapsed:.1f}s: model={resolved_model}, "
            f"url={url}, prompt_chars={len(prompt)}, budget_s={budget:g} ({type(exc).__name__}). "
            f"Ollama answered nothing. Raise MEM_LLM_TIMEOUT if the prompt is simply "
            f"long for this host; a prompt this slow usually means the model is running "
            f"on CPU with no GPU available."
        )
        raise RuntimeError(
            f"LLM call to {resolved_model!r} timed out after {elapsed:.1f}s "
            f"(prompt {len(prompt)} chars, budget {budget:g}s). Ollama is reachable but "
            f"did not finish generating — raise MEM_LLM_TIMEOUT, or lower "
            f"MEM_SCOPE_TEXT_WINDOW / MEM_PEOPLE_WINDOW if the model is too slow for them."
        ) from exc
    except httpx.HTTPError as exc:
        logger.error(
            f"LLM transport error: model={resolved_model}, url={url}, "
            f"prompt_chars={len(prompt)}: {type(exc).__name__}: {exc}"
        )
        raise RuntimeError(
            f"Could not reach Ollama for model {resolved_model!r} at {url}: {exc}. "
            f"Check the ollama container is healthy and the model is present "
            f"(docker exec ollama ollama pull {resolved_model})."
        ) from exc

    logger.warning(
        f"Ollama response: POST /api/chat status={resp.status_code} "
        f"response_chars={len(resp.text)} body={_llm_excerpt(resp.text)}"
    )
    if resp.is_error:
        detail = resp.text.strip()
        logger.error(
            f"LLM request failed: status={resp.status_code}, model={resolved_model}, "
            f"url={url}, response={detail[:500]}"
        )
        raise RuntimeError(
            f"Ollama returned HTTP {resp.status_code} for model {resolved_model!r}: "
            f"{detail[:500]}"
        )
    try:
        content = resp.json()["message"]["content"]
    except (ValueError, KeyError, TypeError) as exc:
        logger.error(
            f"LLM response was not a chat message: model={resolved_model}, "
            f"status={resp.status_code}, body={resp.text[:200]}"
        )
        raise RuntimeError(
            f"Ollama returned an unreadable body for model {resolved_model!r} "
            f"(HTTP {resp.status_code}): {exc}"
        ) from exc
    # Strip any residual <think>…</think> blocks just in case
    import re as _re
    content = _re.sub(r"<think>.*?</think>", "", content, flags=_re.DOTALL).strip()
    if not content:
        # Logged hard, but still returned as "". A model that loaded and produced
        # nothing is the signature of a host with no memory for it, and it used
        # to be completely invisible -- the caller fell back to the raw query or
        # to no keywords and the vault just quietly got worse. Callers have
        # deliberate fallbacks for an empty answer; breaking those would turn a
        # degraded search into a failed request.
        logger.error(
            f"LLM returned no content: model={resolved_model}, "
            f"prompt_chars={len(prompt)}, response_chars={len(resp.text)}, "
            f"elapsed_s={time.monotonic() - started:.1f}. The model answered with "
            f"nothing -- check `docker logs ollama` for memory pressure."
        )
    logger.warning(
        f"Ollama result: chat model={resolved_model} content_chars={len(content)} "
        f"content={_llm_excerpt(content)}"
    )
    return content

# ---------------------------------------------------------------------------
# User extraction
# ---------------------------------------------------------------------------
def extract_user_from_headers(headers: dict) -> str:
    """Work out which vault a request is for.

    Precedence is the trust order, highest first:

      x-vault-user   stamped by McpAuthGuard *after* it authenticated the
                     request. VaultSessionMiddleware strips this header from
                     every inbound request, so a client cannot send its own
                     value — which is why it is allowed to win outright rather
                     than being treated as just another hint.
      Authorization: Bearer   a pre-shared key, resolved against the store.
      Authorization: Basic    the username only; the password is *not* verified
                     here. Anything needing proof verifies it first (McpAuthGuard
                     does, against htpasswd) and stamps x-vault-user.
      proxy identity headers    Remote-User and friends. These are trusted
                     because a reverse proxy is expected to set them, and they
                     are the weakest link in this function: a proxy that
                     forwards a client-supplied Remote-User hands anyone the
                     ability to name any user. Nginx overwrites it from
                     $remote_user; if yours does not, strip it there.
      "anonymous"   the no-evidence answer.

    Returning "anonymous" rather than rejecting is deliberate for the unverified
    callers, which is exactly why McpAuthGuard exists: on the MCP path a
    request that got this far has already been authenticated.
    """
    h = {k.lower(): v for k, v in headers.items()}

    verified = h.get("x-vault-user", "")
    if verified:
        return verified.strip()

    auth = h.get("authorization", "")
    if auth.lower().startswith("bearer "):
        try:
            token = auth.split(None, 1)[1].strip()
        except IndexError:
            token = ""
        if token:
            record = sessions.resolve_psk(token)
            if record:
                return record["user_id"]
        return "anonymous"

    if auth.lower().startswith("basic "):
        try:
            parts = auth.split()
            if len(parts) == 2:
                decoded = base64.b64decode(parts[1]).decode("utf-8")
                if ":" in decoded:
                    return decoded.split(":", 1)[0]
        except Exception:
            pass

    for name in ("remote-user", "x-remote-user", "x-user", "x-forwarded-user"):
        val = h.get(name)
        if val:
            return val

    return "anonymous"
