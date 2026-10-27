"""Pure helpers for Live notes: prompts, validation, budgets and priority.

This module imports **nothing** from the app or from a database driver. That is
the same rule `matching_utils.py` and `sessions.py` follow, and it is what lets
the whole decision surface be *called* by a test on a machine with no Neo4j,
Qdrant, Ollama or FastAPI. The three things that made Live notes hard to get
right -- what the model is allowed to see, what it is allowed to return, and
what happens when the microphone starts while it is thinking -- are all decided
here rather than in the route or the graph layer.

Three invariants are worth stating before the code:

1. **The prompt has exactly four content blocks**: the existing note, the
   segments not yet consumed, the fact titles this note already references, and
   the fact titles the model may reference. Fact *bodies* are never included.
   A note job that leaks a body spends the window on prose the model was not
   asked about, and the user's own note is supposed to be the summary.
2. **A model answer is validated, never trusted.** Unknown fact ids are
   dropped; an empty note is an error; an over-long note is refused rather
   than silently cut, because a truncated note looks complete and loses the
   tail of the conversation.
3. **Audio wins.** `LivePriorityGate` is the in-process statement of that:
   beginning a recording bumps a generation and cancels the **LLM phase** of
   any running job. The commit phase is the caller's responsibility to shield;
   this module only makes the preemption visible.
"""

import json
import math
import os
import re
import threading
from urllib.parse import urlsplit, urlunsplit

# --- tunables -------------------------------------------------------------
# Read at import, like the rest of the app's knobs. Every one of these is also
# forwarded by docker-compose.yml; a value only documented in .env.example is
# unreachable (see test_env_wiring.py).

LIVE_FACT_LIMIT = int(os.getenv("MEM_LIVE_FACT_LIMIT") or 100)
LIVE_CONTEXT_TOKENS = int(os.getenv("MEM_LIVE_CONTEXT_TOKENS") or 16332)
LIVE_MIN_NUM_PREDICT = int(os.getenv("MEM_LIVE_MIN_NUM_PREDICT") or 1500)
LIVE_NOTE_MAX_CHARS = int(os.getenv("MEM_LIVE_NOTE_MAX_CHARS") or 12000)
LIVE_WINDOW = int(os.getenv("MEM_LIVE_WINDOW") or 6000)

# Measured against the merge draft: the char/token ratio drifts downwards as a
# prompt grows (3.21 at 6.7k chars, 3.31 at 43.5k), so the guard rounds down.
# Over-estimating tokens is safe; under-estimating it is what turns a budget
# check into a truncated JSON answer reported as a model failure.
LIVE_PROMPT_CHARS_PER_TOKEN = 3.0

NOTE_TITLE_MAX_CHARS = 200

LIVE_SYSTEM_PROMPT = (
    "You maintain a live note from a meeting transcript. You are given the "
    "note as it stands, only the transcript sections that have not been added "
    "to it yet, the titles of facts the note already references, and the "
    "titles of facts you may reference. Rewrite the note so it incorporates "
    "every new section. Preserve existing detail and Markdown structure; merge "
    "repetition rather than appending a second copy. Never invent names, "
    "decisions, numbers or facts. Reference a fact only by the exact id shown "
    "in the candidate list. If a new section adds nothing, return the note "
    "unchanged. Answer with JSON only, with the keys title, text and "
    "referencedFactIds."
)

# Headings are constants so a test can assert the block boundary rather than
# trusting prose. They are also the only labels the model is given.
BLOCK_NOTE = "EXISTING NOTE"
BLOCK_TRANSCRIPT = "NEW TRANSCRIPT SECTIONS"
BLOCK_REFERENCES = "FACTS THIS NOTE ALREADY REFERENCES"
BLOCK_CANDIDATES = "FACTS YOU MAY REFERENCE"


class LiveNoteError(ValueError):
    """The model's answer cannot be used. Nothing should be consumed."""


class LiveNoteTooLong(LiveNoteError):
    """The note exceeded MEM_LIVE_NOTE_MAX_CHARS; compact, do not slice."""


class RecordingActiveError(RuntimeError):
    """An operation that must not run while the microphone is open."""


def format_timestamp(seconds) -> str:
    """mm:ss, or h:mm:ss past an hour. Stable width keeps prompts readable."""
    try:
        total = max(0, int(float(seconds or 0)))
    except (TypeError, ValueError):
        total = 0
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def segment_text(segment) -> str:
    """The text of one segment, tolerating the dict shape and objects."""
    if isinstance(segment, dict):
        return str(segment.get("text") or "").strip()
    return str(getattr(segment, "text", "") or "").strip()


def segment_start(segment) -> float:
    if isinstance(segment, dict):
        value = segment.get("startSec", segment.get("start", 0))
    else:
        value = getattr(segment, "startSec", getattr(segment, "start", 0))
    try:
        return max(0.0, float(value or 0))
    except (TypeError, ValueError):
        return 0.0


def segment_end(segment) -> float:
    if isinstance(segment, dict):
        value = segment.get("endSec", segment.get("end", 0))
    else:
        value = getattr(segment, "endSec", getattr(segment, "end", 0))
    try:
        return max(0.0, float(value or 0))
    except (TypeError, ValueError):
        return 0.0


def segment_speaker(segment) -> str:
    if isinstance(segment, dict):
        value = segment.get("speaker")
    else:
        value = getattr(segment, "speaker", "")
    return str(value or "Speaker").strip() or "Speaker"


def format_segments(segments) -> str:
    """Render segments as `[mm:ss] Speaker: text`.

    A blank segment is dropped rather than rendered as an empty speaker line:
    asr-mcp emits `empty` turns for silence, and forwarding those as content
    teaches the model that a pause is speech.
    """
    lines = []
    for segment in segments or []:
        text = segment_text(segment)
        if not text:
            continue
        lines.append(f"[{format_timestamp(segment_start(segment))}] "
                     f"{segment_speaker(segment)}: {text}")
    return "\n".join(lines)


def fact_title(fact) -> str:
    """The title of a candidate/reference, never its body."""
    if isinstance(fact, dict):
        value = fact.get("name") or fact.get("title") or ""
    else:
        value = getattr(fact, "name", "") or ""
    return " ".join(str(value or "").split())


def fact_id(fact) -> str:
    if isinstance(fact, dict):
        value = fact.get("id") or ""
    else:
        value = getattr(fact, "id", "") or ""
    return str(value or "").strip()


def _title_tokens(text) -> set:
    """Lowercase alphanumeric tokens of two or more characters.

    Two rather than three keeps short names (`DB`, `HR`) usable; punctuation is
    dropped so `Deutsche Bank (DB)` contributes `deutsche`, `bank` and `db`.
    """
    return {token for token in re.findall(r"[a-z0-9]+", str(text or "").lower())
            if len(token) >= 2}


def rank_fact_candidates(candidates, segments, referenced_ids=(), limit=None):
    """Order candidate facts by title overlap with the new transcript.

    Already-referenced facts are omitted from the result rather than repeated:
    they are rendered in their own prompt block, which is not capped. Dropping
    one because a long pause pushed it past the limit would silently unlink a
    fact the note still discusses.

    Candidates carry id and title only; a body on the input dict is ignored on
    purpose, because the prompt must never contain one.
    """
    cap = LIVE_FACT_LIMIT if limit is None else max(0, int(limit))
    seen = {str(value) for value in (referenced_ids or []) if value}
    transcript = format_segments(segments)
    transcript_lower = transcript.lower()
    transcript_tokens = _title_tokens(transcript)

    scored = []
    for index, candidate in enumerate(candidates or []):
        cid = fact_id(candidate)
        title = fact_title(candidate)
        if not cid or not title or cid in seen:
            continue
        seen.add(cid)
        overlap = len(_title_tokens(title) & transcript_tokens)
        # A full title appearing verbatim is the strongest signal and is worth
        # more than the individual tokens it is made of.
        phrase = title.lower() in transcript_lower
        scored.append((-overlap - (2 if phrase else 0), index, {"id": cid, "name": title}))

    scored.sort(key=lambda item: (item[0], item[1]))
    return [entry for _, _, entry in scored[:cap]]


def _note_parts(note) -> tuple:
    if not note:
        return "", ""
    if isinstance(note, dict):
        return (str(note.get("name") or note.get("title") or "").strip(),
                str(note.get("text") or "").strip())
    return (str(getattr(note, "name", "") or "").strip(),
            str(getattr(note, "text", "") or "").strip())


def build_live_prompt(note, segments, referenced, candidates,
                      client="", context="") -> str:
    """The exact user prompt for one summarise call.

    Only the four blocks above (and a one-line client/project header) are
    included. `segments` is whatever the caller has already narrowed to
    *unconsumed* rows -- this function does not look at consumed state, because
    that is graph state and this module stays dependency-free. Candidate and
    reference dicts are reduced to id/title here, so a caller cannot leak a
    body into the window by passing a full record.
    """
    title, text = _note_parts(note)
    note_block = f"{title}\n\n{text}".strip() if (title or text) else "(no note yet)"
    transcript_block = format_segments(segments) or "(no new speech)"

    reference_lines = [f"- {fact_title(fact)}"
                       for fact in (referenced or []) if fact_title(fact)]
    candidate_lines = [f"- {fact_id(fact)}: {fact_title(fact)}"
                       for fact in (candidates or [])
                       if fact_id(fact) and fact_title(fact)]

    header = f"Client: {str(client or '').strip() or '(none identified)'}\n"
    header += f"Project: {str(context or '').strip() or '(none identified)'}"

    return "\n\n".join([
        header,
        f"### {BLOCK_NOTE}\n{note_block}",
        f"### {BLOCK_TRANSCRIPT}\n{transcript_block}",
        f"### {BLOCK_REFERENCES}\n" + ("\n".join(reference_lines) or "(none)"),
        f"### {BLOCK_CANDIDATES}\n" + ("\n".join(candidate_lines) or "(none)"),
    ])


def _json_object(raw) -> dict:
    """Pull one JSON object out of a model answer, fences and prose included."""
    text = str(raw or "").strip()
    if not text:
        raise LiveNoteError("the model returned an empty answer")
    text = re.sub(r"^\s*```(?:json)?\s*", "", text, flags=re.I)
    text = re.sub(r"\s*```\s*$", "", text)
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        raise LiveNoteError("the model answer contained no JSON object")
    try:
        value = json.loads(text[start:end + 1])
    except (ValueError, TypeError) as exc:
        raise LiveNoteError(f"the model answer was not valid JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise LiveNoteError("the model answer was not a JSON object")
    return value


def parse_note_json(raw, allowed_ids, existing_ids=(), max_chars=None) -> dict:
    """Validate a model answer into `{title, text, referencedFactIds}`.

    Unknown ids are dropped rather than treated as an error: the model naming a
    fact outside the client-scoped candidate list is a reason not to link it,
    not a reason to discard an otherwise good note. Existing references are
    unioned in front of the new ones, so an id the model forgot stays linked.

    An over-long note raises `LiveNoteTooLong` instead of being sliced. A note
    cut at a fixed character count ends mid-sentence and reads as complete,
    which is the silent-loss shape this repository bans for transcripts.
    """
    allowed = {str(value) for value in (allowed_ids or []) if value}
    existing = [str(value) for value in (existing_ids or []) if value]
    limit = LIVE_NOTE_MAX_CHARS if max_chars is None else max(1, int(max_chars))

    data = _json_object(raw)
    text = data.get("text")
    if not isinstance(text, str) or not text.strip():
        raise LiveNoteError("the model answer had no note text")
    text = text.strip()
    if len(text) > limit:
        raise LiveNoteTooLong(
            f"the note is {len(text)} chars, over the {limit}-char limit; "
            "it must be compacted rather than truncated"
        )

    title = data.get("title")
    if not isinstance(title, str) or not title.strip():
        title = "Live notes"
    title = " ".join(title.split())[:NOTE_TITLE_MAX_CHARS]

    raw_refs = data.get("referencedFactIds")
    if isinstance(raw_refs, str):
        raw_refs = [raw_refs]
    if not isinstance(raw_refs, (list, tuple)):
        raw_refs = []

    ordered = []
    for cid in list(existing) + list(raw_refs):
        cid = str(cid or "").strip()
        if cid and cid in allowed and cid not in ordered:
            ordered.append(cid)

    return {"title": title, "text": text, "referencedFactIds": ordered}


COMPACT_SYSTEM_PROMPT = """You condense a live meeting note that has grown too long.

Return one JSON object with exactly `title` and `text`.

Rules:
- Keep the note's headings, bullets and factual detail. This is a condensation,
  not a new summary: do not drop decisions, owners, numbers or open questions.
- Remove repetition and filler first. Merge bullets that say the same thing.
- Never invent anything and never add a section that is not already present.
- `title` must stay recognisable as the same meeting note.
- `text` must be strictly shorter than the note you were given and must fit
  inside the target length.
- Return JSON only. No markdown fences, no commentary.
"""


def build_compaction_prompt(note, max_chars=None) -> str:
    """The prompt for the one compaction pass, with its target stated.

    Compaction is a separate call because a note at the character limit cannot
    absorb another window: the summarise answer would be rejected as too long
    and the window would be lost. The target is a real number rather than
    "shorter", because a model asked to shorten by an unspecified amount
    usually returns something a few characters shorter and the next window
    fails for the same reason.
    """
    limit = LIVE_NOTE_MAX_CHARS if max_chars is None else max(1, int(max_chars))
    title, text = _note_parts(note)
    target = max(1, int(limit * 0.7))
    return "\n\n".join([
        f"Target length: at most {target} characters (the hard limit is {limit}).",
        f"### NOTE TITLE\n{title or '(untitled)'}",
        f"### NOTE TEXT\n{text or '(empty)'}",
    ])


def estimate_prompt_tokens(prompt_chars) -> int:
    """Conservative token estimate for a prompt, rounded up."""
    try:
        chars = max(0, int(prompt_chars))
    except (TypeError, ValueError):
        chars = 0
    return int(math.ceil(chars / LIVE_PROMPT_CHARS_PER_TOKEN))


def live_output_budget(prompt_chars, context_tokens=None, min_predict=None) -> dict:
    """How many output tokens are left after the prompt.

    Returns a dict so the caller can refuse with the same numbers it computed:
    `{ok, prompt_tokens, budget, floor, reason}`. The floor is inclusive -- a
    budget exactly equal to `min_predict` is allowed -- because a boundary that
    is off by one token is indistinguishable from a working guard until the
    answer is cut off.
    """
    context = LIVE_CONTEXT_TOKENS if context_tokens is None else max(1, int(context_tokens))
    floor = LIVE_MIN_NUM_PREDICT if min_predict is None else max(1, int(min_predict))
    prompt_tokens = estimate_prompt_tokens(prompt_chars)
    budget = context - prompt_tokens
    if budget < floor:
        return {
            "ok": False,
            "prompt_tokens": prompt_tokens,
            "budget": max(0, budget),
            "floor": floor,
            "reason": (f"the prompt is {prompt_tokens} tokens of a {context}-token "
                       f"window, leaving {max(0, budget)} for the note; at least "
                       f"{floor} are needed, so select less text or raise "
                       f"MEM_LIVE_CONTEXT_TOKENS"),
        }
    return {
        "ok": True,
        "prompt_tokens": prompt_tokens,
        "budget": budget,
        "floor": floor,
        "reason": None,
    }


def segment_windows(segments, window=None):
    """Batch segments into windows without ever splitting one segment.

    A single segment longer than the window is returned as its own batch: half
    a sentence is not a smaller unit of meaning, and dropping the tail is the
    failure mode windowing exists to prevent. Overlap is deliberately absent --
    unlike free text, a segment is a complete unit, so repeating one would make
    the model write the same paragraph into the note twice.
    """
    size = LIVE_WINDOW if window is None else max(1, int(window))
    batches = []
    current = []
    used = 0
    for segment in segments or []:
        text = segment_text(segment)
        if not text:
            continue
        line_length = len(text) + len(segment_speaker(segment)) + 12
        if current and used + line_length > size:
            batches.append(current)
            current = []
            used = 0
        current.append(segment)
        used += line_length
    if current:
        batches.append(current)
    return batches


def with_time_offset(segment, offset):
    """A copy of one segment with its connection-relative times shifted.

    asr-mcp's timestamps restart at zero on every upstream connection, and a
    Live notes session pauses by reconnecting. Without the stored offset a
    resumed segment appears to come from the start of the meeting again.
    """
    try:
        shift = max(0.0, float(offset or 0))
    except (TypeError, ValueError):
        shift = 0.0
    if isinstance(segment, dict):
        result = dict(segment)
        result["startSec"] = segment_start(segment) + shift
        result["endSec"] = segment_end(segment) + shift
        return result
    result = dict(getattr(segment, "__dict__", {}) or {})
    result["startSec"] = segment_start(segment) + shift
    result["endSec"] = segment_end(segment) + shift
    return result


def offset_segments(segments, offset):
    return [with_time_offset(segment, offset) for segment in (segments or [])]


def asr_segment(payload, offset=0.0):
    """Map one asr-mcp message to the segment this app stores.

    Returns ``None`` for anything that is not a non-empty ``transcript``.
    asr-mcp timestamps restart at zero on every connection, so the caller adds
    the stored maximum end offset; a segment written on a resumed socket
    without it would appear at the beginning of the recording and the next
    pause would summarise the transcript out of order.

    ``end`` is preferred, with ``start + duration`` as the fallback. An
    inverted or missing pair is clamped to ``start`` rather than raising,
    because a malformed timing field must not drop the words.
    """
    if not isinstance(payload, dict) or payload.get("type") != "transcript":
        return None
    text = str(payload.get("text") or "").strip()
    if not text:
        return None
    try:
        start_raw = float(payload.get("start") or 0.0)
        end_raw = payload.get("end")
        if end_raw is None:
            end_raw = start_raw + float(payload.get("duration") or 0.0)
        start = max(0.0, start_raw + float(offset or 0.0))
        end = max(start, float(end_raw) + float(offset or 0.0))
    except (TypeError, ValueError):
        return None
    return {
        "text": text,
        "speaker": str(payload.get("speaker") or ""),
        "startSec": start,
        "endSec": end,
    }


def _origin_of(url) -> str:
    """scheme://host[:port] for an absolute URL, else ''."""
    value = str(url or "").strip()
    if not value:
        return ""
    try:
        parts = urlsplit(value)
    except ValueError:
        return ""
    if not parts.scheme or not parts.netloc:
        return ""
    return f"{parts.scheme.lower()}://{parts.netloc.lower()}"


def origin_allowed(origin, base_url, request_origin="") -> bool:
    """Whether a WebSocket handshake's Origin may open a recording socket.

    `BASE_URL` includes the proxy mount (`https://host/mem-mcp`), so only its
    scheme and host are compared. An **absent** Origin is allowed: a browser
    always sends one, so its absence means a scripted client, which cannot be
    the target of cross-site WebSocket hijacking and still has to present a
    session cookie. A literal `null` Origin is not absent -- sandboxed iframes
    send it -- and is refused.

    When `BASE_URL` is relative or unset there is no origin to compare against.
    The caller may pass the request's own origin as `request_origin`; without
    one, a supplied Origin is refused rather than waved through.
    """
    value = str(origin or "").strip().lower().rstrip("/")
    if value == "":
        return True
    if value == "null":
        return False
    expected = _origin_of(base_url) or _origin_of(request_origin)
    return bool(expected) and value == expected


def asr_languages_url(ws_url: str) -> str:
    """The HTTP URL that validates an asr-mcp key without loading a model.

    `MEM_ASR_WS_URL` points at the streaming socket, and asr-mcp loads the
    recogniser when a socket opens: checking whether a key works therefore cost
    a model load on a 4 GB card that Ollama is also using. `GET
    /api/asr/languages` sits behind the same auth middleware and answers from a
    static list, so a key can be verified for the price of an HTTP round trip.

    Derived from the socket URL rather than configured separately -- two knobs
    naming the same server drift, and the one that is wrong is the one nobody
    edits. Returns `""` when no URL is configured, which the caller reports as
    unreachable.
    """
    parts = urlsplit(str(ws_url or "").strip())
    if not parts.netloc:
        return ""
    scheme = {"ws": "http", "wss": "https"}.get(parts.scheme, "http")
    path = parts.path or "/"
    if "/ws/" in path:
        path = path.split("/ws/", 1)[0]
    else:
        path = path.rsplit("/", 1)[0]
    path = path.rstrip("/") + "/languages"
    return urlunsplit((scheme, parts.netloc, path, "", ""))


class LivePriorityGate:
    """Per-user recording state and the generation counter that preempts jobs.

    A note job registers a cancellation callback **only while it is inside the
    LLM call**. `begin_recording` invokes that callback and clears it, so audio
    starting during generation stops the model run; audio starting during the
    commit does not, because the commit is the moment Qdrant and Neo4j must not
    be torn apart. The caller shields the commit and re-checks the generation
    immediately before it.

    The counter exists because a callback can race: a job that was between two
    windows when the recording began has no cancellation registered, so it
    compares the generation it captured with the current one before doing any
    further work.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._state = {}

    def _entry(self, user_id):
        user = str(user_id or "").strip()
        if not user:
            raise ValueError("Live notes priority needs a user")
        state = self._state.get(user)
        if state is None:
            state = {"recording": False, "generation": 0, "cancel_llm": None}
            self._state[user] = state
        return state

    def begin_recording(self, user_id) -> int:
        with self._lock:
            state = self._entry(user_id)
            state["recording"] = True
            state["generation"] += 1
            callback = state["cancel_llm"]
            state["cancel_llm"] = None
            generation = state["generation"]
        if callback is not None:
            try:
                callback()
            except Exception:  # noqa: BLE001 -- audio must not fail on a job's bug
                pass
        return generation

    def end_recording(self, user_id) -> None:
        with self._lock:
            self._entry(user_id)["recording"] = False

    def current_generation(self, user_id) -> int:
        with self._lock:
            return self._entry(user_id)["generation"]

    def is_recording(self, user_id) -> bool:
        with self._lock:
            return bool(self._entry(user_id)["recording"])

    def assert_idle(self, user_id) -> None:
        if self.is_recording(user_id):
            raise RecordingActiveError(
                "Recording is in progress. Pause the microphone before updating "
                "notes; audio always takes priority over processing."
            )

    def register_llm_cancel(self, user_id, callback) -> bool:
        """Register the LLM-phase cancel hook. False when audio already started."""
        with self._lock:
            state = self._entry(user_id)
            if state["recording"]:
                return False
            state["cancel_llm"] = callback
            return True

    def clear_llm_cancel(self, user_id, callback=None) -> None:
        with self._lock:
            state = self._entry(user_id)
            if callback is None or state["cancel_llm"] is callback:
                state["cancel_llm"] = None
