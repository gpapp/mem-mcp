"""
status_monitor.py – server status snapshots and their change broadcast.

Dependency-light on purpose: standard library only, nothing from ``common.py``
and no ``httpx``, so the pure half can be unit-tested on a host with no
services running (the same constraint ``matching_utils.py`` and
``chunking.py`` are under). ``common.py`` owns the HTTP calls and hands the
parsed payloads in; this module turns them into a snapshot, decides whether
that snapshot differs from the last one, and holds the subscriber queues the
SSE stream reads from.

Two decisions here are load-bearing, and both are the reason this is a module
rather than a helper inside ``gui.py``:

- **The change test excludes the wall clock.** The status widget is pushed
  over SSE "when something changes", and a fingerprint taken over the whole
  snapshot would include ``checked`` and a keep-alive countdown — so every
  poll would look like a change and the stream would push sixty times a
  minute to say nothing. ``signature()`` covers state only; the countdown is
  rendered and ticked in the browser off an absolute timestamp.
- **A resident model is reported with its GPU share, derived here.** Ollama's
  ``/api/ps`` returns the two byte counts and no processor field, so
  "100% GPU" has to be recomputed with the same rule ``ollama ps`` prints. On
  a host where the GPU compose overlay may have been dropped this is the only
  honest signal, and it is the check AGENTS.md tells operators to make.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger("memory-vault.status")

# Display and sort order for the configured roles. It is deliberately *not*
# alphabetical: the embedder is first because it is resident on nearly every
# write, and the merge model is last because it is the one an operator is most
# likely to want to evict.
ROLE_ORDER = ("embedder", "extract", "query", "scope", "merge")

# Snapshot keys deliberately left out of the fingerprint. Both move on their
# own between two identical observations, so including either would make every
# poll a "change". Keep this list and `signature()` in step: a new key that
# describes state belongs in the fingerprint, a new key that describes time
# does not.
VOLATILE_SNAPSHOT_KEYS = ("checked", "expiresAt")

# Per-model keys that make up the fingerprint, in the order they are read.
_SIGNATURE_MODEL_KEYS = (
    "name",
    "roles",
    "configured",
    "installed",
    "resident",
    "size",
    "sizeVram",
    "processor",
    "family",
    "quantization",
)

# Fields `/api/tags` and `/api/ps` use for the model name, newest first. Ollama
# has used both across versions and a status widget that silently drops every
# model because it looked for the wrong key is indistinguishable from a vault
# with no models configured.
_NAME_KEYS = ("name", "model")


def _entry_name(entry: Any) -> str:
    if not isinstance(entry, dict):
        return ""
    for key in _NAME_KEYS:
        value = entry.get(key)
        if value:
            return str(value)
    return ""


def _as_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def model_roles(
    embedder: str = "",
    query: str = "",
    extract: str = "",
    scope: str = "",
    merge: str = "",
) -> List[Dict[str, str]]:
    """Pair each configured role with the model it resolves to, in ROLE_ORDER.

    One entry per *role*, not per model: two roles sharing a model is the
    documented default (``MEM_EXTRACT_MODEL`` falls back to
    ``LLM_QUERY_MODEL``), and collapsing them here would drop the second role
    from the UI — the very thing the operator is looking at when deciding
    whether to evict a model.
    """
    wanted = {
        "embedder": embedder,
        "extract": extract,
        "query": query,
        "scope": scope,
        "merge": merge,
    }
    roles: List[Dict[str, str]] = []
    for role in ROLE_ORDER:
        model = str(wanted.get(role) or "").strip()
        if not model:
            continue
        roles.append({"role": role, "model": model})
    return roles


def processor_label(size: Any, size_vram: Any) -> str:
    """Recompute the processor label `ollama ps` prints, because the API omits it.

    ``/api/ps`` reports ``size`` (whole model) and ``size_vram`` (the part
    offloaded to the GPU) and nothing else. The CLI derives the label from
    exactly those two numbers: no VRAM is CPU inference, VRAM covering the
    whole model is GPU, anything between is a split. Keeping the CPU share
    first matches the CLI's own column order.
    """
    total = _as_int(size)
    vram = _as_int(size_vram)
    if total <= 0:
        return ""
    if vram <= 0:
        return "100% CPU"
    if vram >= total:
        return "100% GPU"
    gpu = round(vram * 100 / total)
    return f"{100 - gpu}%/{gpu}% CPU/GPU"


def _role_rank(model: Dict[str, Any]) -> int:
    for index, role in enumerate(ROLE_ORDER):
        if role in (model.get("roles") or ()):
            return index
    return len(ROLE_ORDER)


def build_status(
    *,
    roles: Optional[List[Dict[str, str]]] = None,
    url: str = "",
    version: Any = None,
    tags: Any = None,
    ps: Any = None,
    error: Optional[str] = None,
    warnings: Optional[List[str]] = None,
    maintenance: Optional[Dict[str, str]] = None,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Fold the three Ollama payloads into one snapshot.

    ``error`` means nothing answered and the widget should show the service
    down; ``warnings`` means something answered and the widget should show it
    degraded. Collapsing the two would either hide a broken endpoint behind a
    green light or paint the whole service red because one route moved.
    """
    roles = list(roles or [])

    installed: Dict[str, Dict[str, Any]] = {}
    for entry in ((tags or {}).get("models") or []):
        name = _entry_name(entry)
        if name:
            installed[name] = entry
    resident: Dict[str, Dict[str, Any]] = {}
    for entry in ((ps or {}).get("models") or []):
        name = _entry_name(entry)
        if name:
            resident[name] = entry

    by_model: Dict[str, List[str]] = {}
    for role in roles:
        by_model.setdefault(role["model"], []).append(role["role"])

    models: List[Dict[str, Any]] = []
    for name in set(installed) | set(resident) | set(by_model):
        tagged = installed.get(name) or {}
        running = resident.get(name) or {}
        details = running.get("details") or tagged.get("details") or {}
        size = _as_int(running.get("size")) or _as_int(tagged.get("size"))
        size_vram = _as_int(running.get("size_vram"))
        models.append({
            "name": name,
            "roles": by_model.get(name, []),
            "configured": name in by_model,
            "installed": name in installed,
            "resident": name in resident,
            "size": size,
            "sizeVram": size_vram,
            # Only a resident model is on a processor at all. Deriving the
            # label from the installed-model byte count would label every cold
            # model "100% CPU", which reads as a running model on the CPU.
            "processor": processor_label(size, size_vram) if name in resident else "",
            "expiresAt": running.get("expires_at") or None,
            "family": details.get("family") or None,
            "quantization": details.get("quantization_level") or None,
        })

    # Resident first, then configured-role order, then alphabetical. A widget
    # that lists a cold model above a resident one reads as "the loaded model
    # is missing" at a glance.
    models.sort(key=lambda m: (not m["resident"], _role_rank(m), m["name"]))

    version_body = version if isinstance(version, dict) else {}
    answered = bool(version_body) or ps is not None or tags is not None
    return {
        "ok": error is None and answered,
        "url": url,
        "version": version_body.get("version") or None,
        "error": error,
        "warnings": list(warnings or []),
        "checked": (now or datetime.now(timezone.utc)).isoformat(),
        "models": models,
        "installedCount": len(installed),
        "residentCount": sum(1 for m in models if m["resident"]),
        "maintenance": dict(maintenance or {}),
    }


def signature(snapshot: Dict[str, Any]) -> str:
    """A stable fingerprint of the *state* in a snapshot.

    Everything here is a field the widget actually draws. The two keys left
    out move on their own between two identical observations — ``checked`` is
    a wall clock and ``expiresAt`` is a keep-alive countdown — so a naive
    fingerprint over the whole dict turns a quiet poller into a push every
    tick. The countdown is the browser's problem: it gets an absolute
    timestamp and renders it locally.
    """
    payload = {
        "ok": snapshot.get("ok"),
        "version": snapshot.get("version"),
        "error": snapshot.get("error"),
        "warnings": snapshot.get("warnings") or [],
        "maintenance": snapshot.get("maintenance") or {},
        "models": [
            {key: model.get(key) for key in _SIGNATURE_MODEL_KEYS}
            for model in snapshot.get("models") or []
        ],
    }
    return json.dumps(payload, sort_keys=True, default=str)


# ---------------------------------------------------------------------------
# Subscriber plumbing
#
# Module-level rather than per-connection on purpose: the poller is one task
# for the whole process, and N browser tabs must not mean N polls of Ollama.
# ---------------------------------------------------------------------------
_subscribers: List[asyncio.Queue] = []
_snapshot: Optional[Dict[str, Any]] = None
_snapshot_signature: Optional[str] = None
_publish_lock = asyncio.Lock()


def subscribe() -> asyncio.Queue:
    """Register a queue for status pushes. Dropped at most one snapshot deep."""
    queue: asyncio.Queue = asyncio.Queue(maxsize=1)
    _subscribers.append(queue)
    return queue


def unsubscribe(queue: asyncio.Queue) -> None:
    if queue in _subscribers:
        _subscribers.remove(queue)


def subscriber_count() -> int:
    return len(_subscribers)


def last_snapshot() -> Optional[Dict[str, Any]]:
    return _snapshot


def _store(snapshot: Dict[str, Any]) -> bool:
    """Remember a snapshot. True if it differs from the one already held."""
    global _snapshot, _snapshot_signature
    fingerprint = signature(snapshot)
    if _snapshot is not None and fingerprint == _snapshot_signature:
        return False
    _snapshot = snapshot
    _snapshot_signature = fingerprint
    return True


async def publish(snapshot: Dict[str, Any]) -> bool:
    """Store a snapshot and push it to subscribers when it changed.

    Returns whether anything was pushed. Bounded queues that are already full
    lose their oldest item rather than blocking: the next poll replaces the
    content, and a status widget one tick stale is much cheaper than a poller
    wedged on a browser that stopped reading.
    """
    async with _publish_lock:
        if not _store(snapshot):
            return False
        for queue in list(_subscribers):
            try:
                if queue.full():
                    queue.get_nowait()
                queue.put_nowait(_snapshot)
            except asyncio.QueueEmpty:  # pragma: no cover - racy drain
                queue.put_nowait(_snapshot)
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning(f"status subscriber push failed: {type(exc).__name__}: {exc}")
        return True


def reset() -> None:
    """Forget the stored snapshot and drop every subscriber (tests only)."""
    global _snapshot, _snapshot_signature
    _snapshot = None
    _snapshot_signature = None
    del _subscribers[:]


async def broadcast_loop(probe, interval: float) -> None:
    """Poll ``probe()`` and publish on change, until cancelled.

    Started once in the server lifespan. The first poll is immediate so a page
    that loads just after startup finds a snapshot instead of waiting a whole
    interval for one.
    """
    delay = max(1.0, float(interval or 0))
    while True:
        try:
            await publish(await probe())
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(f"status probe failed: {type(exc).__name__}: {exc}")
        await asyncio.sleep(delay)
