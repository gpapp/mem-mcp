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
# Model used for server-side scope classification (client/context backfill).
# Override with MEM_SCOPE_MODEL if a more capable model is available in Ollama.
SCOPE_MODEL = os.getenv("MEM_SCOPE_MODEL") or LLM_QUERY_MODEL
# Set MEM_SCOPE_BACKFILL=0 to skip the LLM scope backfill pass at startup.
SCOPE_BACKFILL_ENABLED = os.getenv("MEM_SCOPE_BACKFILL", "1") == "1"
SCOPE_BACKFILL_CONCURRENCY = int(os.getenv("MEM_SCOPE_CONCURRENCY", "3"))
HTTP_TIMEOUT    = float(os.getenv("MEM_HTTP_TIMEOUT", "300.0"))
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
            _qdrant = AsyncQdrantClient(url=QDRANT_URL)
        
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


async def ensure_ollama_models() -> None:
    """Ensure every configured Ollama model is available before startup work."""
    models = list(dict.fromkeys((EMBED_MODEL, LLM_QUERY_MODEL, SCOPE_MODEL)))
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
            if model in installed:
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
async def get_embedding(text: str) -> List[float]:
    request_body = {"model": EMBED_MODEL, "prompt": text}
    logger.warning(f"Ollama request: POST {OLLAMA_URL}/api/embeddings body={request_body}")
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
        resp = await client.post(
            f"{OLLAMA_URL}/api/embeddings",
            json=request_body,
        )
        logger.warning(
            f"Ollama response: POST /api/embeddings status={resp.status_code} body={resp.text}"
        )
        resp.raise_for_status()
        embedding = resp.json()["embedding"]
        logger.warning(f"Ollama result: embedding model={EMBED_MODEL} dimensions={len(embedding)}")
        return embedding


async def get_llm_response(prompt: str, system: str = "", model: str = "", num_predict: int = 0) -> str:
    """Call Ollama /api/chat and return the assistant's text response.

    Uses LLM_QUERY_MODEL (default: qwen3.5:0.8b) unless overridden by `model`.
    Times out after 60 s — intentionally short for interactive search calls.
    Strips <think>…</think> blocks produced by reasoning models (e.g. Qwen3).
    Pass num_predict > 0 to cap/guarantee the output token budget.
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
    logger.warning(f"Ollama request: POST {OLLAMA_URL}/api/chat body={request_body}")
    async with httpx.AsyncClient(timeout=60.0) as client:
        resp = await client.post(
            f"{OLLAMA_URL}/api/chat",
            json=request_body,
        )
        logger.warning(
            f"Ollama response: POST /api/chat status={resp.status_code} body={resp.text}"
        )
        if resp.is_error:
            detail = resp.text.strip()
            logger.error(
                f"LLM request failed: status={resp.status_code}, model={resolved_model}, "
                f"url={OLLAMA_URL}/api/chat, response={detail[:500]}"
            )
            resp.raise_for_status()
        content = resp.json()["message"]["content"]
        # Strip any residual <think>…</think> blocks just in case
        import re as _re
        content = _re.sub(r"<think>.*?</think>", "", content, flags=_re.DOTALL).strip()
        logger.warning(f"Ollama result: chat model={resolved_model} content={content}")
        return content

# ---------------------------------------------------------------------------
# User extraction
# ---------------------------------------------------------------------------
def extract_user_from_headers(headers: dict) -> str:
    h = {k.lower(): v for k, v in headers.items()}

    auth = h.get("authorization", "")
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
