#!/usr/bin/env python3
"""model_bench.py -- evaluate every installed chat model against every app role.

The deployment splits four chat roles across two models, and that split was
decided by hand-measured comparisons (see AGENTS.md, "Critical Config").  This
script makes the comparison repeatable: each installed chat model is run against
each role's *real* prompt shape, on several samples, with thinking on and off,
and the outputs are reported so a human -- not a regex -- can judge them.

Why the prompts are lifted from source rather than copied
--------------------------------------------------------
Four of the six roles have their system prompt as a module-level string (or a
local assigned once) in the module that ships it.  `_lift_*` reads those with
`ast`, so what is measured is the prompt the app actually sends.  A copy in
here would drift, and a drifted prompt produces a benchmark that recommends a
model for a question nobody asks.  If a prompt moves, the lift FAILS LOUDLY
rather than silently falling back to a stale copy.

The two roles that cannot be lifted -- `merge` and `live` -- are reproduced at
contract level (JSON `{title, text}` from N records; Markdown note from
transcript segments), because their real prompts are assembled from graph
candidates this script has no access to.  The report says so.

What it does NOT do
-------------------
It does not decide anything.  Every role's answer is also scored objectively
(JSON validity, name recall, retention, filter echo, degenerate decode) because
those numbers are stable, but the judgement that matters -- "is this scope
verdict actually right", "is this note about the meeting" -- is left in the raw
output for a reader, and the report prints all of it.

Sampling is per model, on purpose
---------------------------------
This app pins `temperature: 0.0` for every call (common.py, `get_llm_response`).
A model tuned for temperature 0.7-1.0 is being asked to do something it was not
trained for at 0.0, and running it at 0.0 would measure the harness rather than
the model.  `sampling_for()` therefore sends each model the sampling it is meant
to be run at, and the report records which.  That asymmetry is the honest one:
each model is measured as it would actually be deployed.

Usage
-----
    # plan only, no network
    python3 model_bench.py --dry-run

    # self-test the whole pipeline against a stub backend
    python3 model_bench.py --stub --out /tmp/bench

    # the real thing, from inside the app container
    python3 model_bench.py --out /app/logs/bench --samples 3

stdlib only (urllib/json/ast/argparse/time/statistics/re/shutil), so it runs on
a host with no database drivers and no web framework.
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import re
import statistics
import sys
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_URL = os.getenv("MEM_LLM_URL", "http://ollama:11434")

# nomic-embed-text is an embedding model. Pointing a chat-role benchmark at it
# would produce a number that means nothing, so it is excluded by name rather
# than by guessing from a capability list.
EMBEDDER_HINT = os.getenv("MEM_EMBEDDER_MODEL", "nomic-embed-text").split(":")[0]

# The window every model is measured at. This is the single knob that made the
# first run compare models on unequal footing: Ollama falls back to the model's
# own default `num_ctx` (4096 on the MiniCPM5 tag), so one model answered the
# same prompt with a quarter of the context another had. It is set explicitly
# here and recorded in the report, because the merge path needs 16332 tokens and
# a model measured at 4096 would look like it had truncated an answer it never
# saw all of.
DEFAULT_NUM_CTX = int(os.getenv("OLLAMA_CONTEXT_LENGTH", "16332"))

ROLES = ("scope", "keywords", "people", "people_bind", "search_rewrite",
         "merge", "live")

# Vendor guidance, not taste -- and three of these five families disagree with
# what this harness sent before the cards were read.  Every value below is
# quoted from the model's own Hugging Face card (`/raw/main/README.md`); where a
# card says nothing, the app's greedy decode is kept and marked as a decision
# rather than a recommendation.
#
#   MiniCPM5-2B    "temperature=1.0, top_p=0.95, min_p=0.0" -- ONE set, with no
#                  think / no-think distinction anywhere on the card.  The
#                  0.9 / 0.7 this harness used appears nowhere in it.  The card
#                  also warns that llama.cpp's default `min_p=0.05` "can lead to
#                  repetitive output", so min_p is pinned to 0.0 explicitly.
#   LFM2.5-2.6B    temperature=0.1, top_k=50, repetition_penalty=1.1, and
#                  "always thinks before it answers" -- the chat template
#                  inserts the `<think>` tag -- so both modes are the same.
#   Nemotron-3-Nano-4B
#                  temperature=1.0 + top_p=0.95 for *reasoning*, 0.6 + 0.95 for
#                  *tool calling*.  It is never greedy.  think=True takes the
#                  reasoning row; think=False is a direct answer rather than a
#                  reasoning trace, so it takes the 0.6 row -- the card's only
#                  guidance for output that is not a reasoning trace.
#   Gemma 4 E2B    temperature=1.0, top_p=0.95, top_k=64.
#   Granite 3.3 2B its README states **no sampling at all** -- no temperature,
#                  top_p, top_k or penalty anywhere in it -- so `default` keeps
#                  the app's greedy 0.0.  That is an app decision, not a vendor
#                  value, and granite is now the only family run that way.
_SAMPLING = {
    "minicpm": {
        "think": {"temperature": 1.0, "top_p": 0.95, "min_p": 0.0},
        "nothink": {"temperature": 1.0, "top_p": 0.95, "min_p": 0.0},
    },
    "lfm": {
        "think": {"temperature": 0.1, "top_k": 50, "repeat_penalty": 1.1},
        "nothink": {"temperature": 0.1, "top_k": 50, "repeat_penalty": 1.1},
    },
    "nemotron": {
        "think": {"temperature": 1.0, "top_p": 0.95},
        "nothink": {"temperature": 0.6, "top_p": 0.95},
    },
    "gemma": {
        "think": {"temperature": 1.0, "top_p": 0.95, "top_k": 64},
        "nothink": {"temperature": 1.0, "top_p": 0.95, "top_k": 64},
    },
    "default": {
        "think": {"temperature": 0.0},
        "nothink": {"temperature": 0.0},
    },
}

# Production does not sample uniformly either. The two extractors and the
# live-note rewrite go through `get_extraction_response`, which sends
# `EXTRACT_SAMPLING` (repeat_penalty 1.15 over the last 256 tokens) because a
# greedy decode of `granite3.3:2b` loops until Ollama aborts the prediction with
# "token repeat limit reached".  The first run of this harness omitted it and
# produced eight such failures that were the benchmark's fault, not the
# model's -- so the role carries its own penalty, lifted from `common.py` when
# possible and pinned here only as a fallback.
_ROLE_SAMPLING = {
    "keywords": {"repeat_penalty": 1.15, "repeat_last_n": 256},
    "people": {"repeat_penalty": 1.15, "repeat_last_n": 256},
    "live": {"repeat_penalty": 1.15, "repeat_last_n": 256},
}

# A thinking model spends its budget inside the reasoning block before it emits
# any content, and the production budgets are small on purpose (scope asks for
# 80 tokens).  The first run therefore produced an empty content field for
# nearly every `think=True` call -- the model answered, it just never reached
# the answer.  The bonus buys the reasoning room; the production budget is still
# what the content itself is allowed to use, so a model that would overrun
# `num_predict` in production still shows it.
THINK_BUDGET_BONUS = 1500


# ---------------------------------------------------------------------------
# Ollama client
# ---------------------------------------------------------------------------
class OllamaError(RuntimeError):
    pass


def _post(url: str, body: dict, timeout: float) -> dict:
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:400]
        raise OllamaError(f"HTTP {exc.code}: {detail}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise OllamaError(f"{type(exc).__name__}: {exc}") from exc


def _get(url: str, timeout: float) -> dict:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:400]
        raise OllamaError(f"HTTP {exc.code}: {detail}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise OllamaError(f"{type(exc).__name__}: {exc}") from exc


def list_models(base_url: str, timeout: float = 20.0) -> list:
    body = _get(f"{base_url.rstrip('/')}/api/tags", timeout)
    names = [m.get("name") or m.get("model") or "" for m in body.get("models", [])]
    return [n for n in names if n and EMBEDDER_HINT not in n]


def chat(base_url: str, model: str, system: str, prompt: str, *,
         think: bool, options: dict, num_predict: int = 0,
         num_ctx: int = DEFAULT_NUM_CTX, timeout: float = 300.0) -> dict:
    """One /api/chat call, returning the answer *and* Ollama's own timing fields.

    `think` is passed explicitly rather than left to the model default, because
    the whole point of the harness is to compare the two modes. A model with no
    thinking capability answers this with a 400; that is a real finding and the
    caller records it rather than aborting the run.

    `num_ctx` is passed explicitly for the same reason: a model whose tag
    defaults to 4096 would otherwise answer a 6k-character prompt with four
    thousand tokens of window while its rival used 16332, and the comparison
    would be between two different questions.
    """
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})
    opts = dict(options)
    if num_predict > 0:
        opts["num_predict"] = num_predict
    if num_ctx > 0:
        opts["num_ctx"] = num_ctx
    body = {
        "model": model,
        "messages": messages,
        "stream": False,
        "think": think,
        "options": opts,
    }
    started = time.monotonic()
    raw = _post(f"{base_url.rstrip('/')}/api/chat", body, timeout)
    elapsed = time.monotonic() - started
    msg = raw.get("message") or {}
    return {
        "content": (msg.get("content") or "").strip(),
        "thinking": (msg.get("thinking") or "").strip(),
        "seconds": round(elapsed, 2),
        "prompt_tokens": raw.get("prompt_eval_count"),
        "answer_tokens": raw.get("eval_count"),
        "done_reason": raw.get("done_reason"),
    }


_FAMILIES = ("minicpm", "lfm", "nemotron", "gemma")


def family_for(model: str) -> str:
    """Which vendor-sampling family a model tag belongs to.

    Its own function, and not inlined into `sampling_for`, because a family the
    scan misses falls back to `"default"` -- greedy 0.0 -- silently.  That is the
    one mistake here that reads as a model result rather than a harness bug, so
    it has to be assertable on its own.
    """
    lowered = model.lower()
    for name in _FAMILIES:
        if name in lowered:
            return name
    return "default"


def sampling_for(model: str, think: bool, role: str = "") -> dict:
    """The sampling this model would actually be run at, in this role.

    Two things compose, and the order is deliberate.  `_ROLE_SAMPLING` is what
    production sends for that role -- the repeat penalty that stops
    `granite3.3:2b` looping until Ollama aborts the prediction -- so it applies
    to every model, because every model would be handed it.  The family dict is
    what the *model* needs; it is merged last so a vendor requirement (MiniCPM5's
    temperature 1.0, LFM2.5's 0.1) is not silently overridden by the penalty, and
    it wins only on the keys it actually sets.  Dropping the role penalty is what
    made the first run report eight repeat-aborts as model failures.
    """
    family = family_for(model)
    options = dict(_ROLE_SAMPLING.get(role, {}))
    options.update(_SAMPLING[family]["think" if think else "nothink"])
    return options


# ---------------------------------------------------------------------------
# Lifting the shipping prompts
# ---------------------------------------------------------------------------
class PromptDrift(RuntimeError):
    pass


def _parse(module: str) -> ast.Module:
    path = os.path.join(HERE, module)
    with open(path, "r", encoding="utf-8") as handle:
        return ast.parse(handle.read(), filename=path)


def _module_string(module: str, name: str) -> str:
    """A module-level `NAME = "..."`, read without importing the module.

    `migrate_client_context.py` and `diary_manager.py` both import the DB
    drivers, so they cannot be imported here. The constant is still the same
    text object the app sends, because implicit string concatenation is folded
    into one Constant by the parser.
    """
    for node in _parse(module).body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == name:
                    value = ast.literal_eval(node.value)
                    if isinstance(value, str):
                        return value
    raise PromptDrift(
        f"{name} is not a module-level string in {module}. The prompt moved; "
        f"update model_bench.py rather than letting a stale copy be measured.")


def _local_string(module: str, function: str, name: str) -> str:
    """A local `name = "..."` inside one function (the search rewriter's system)."""
    for node in _parse(module).body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == function:
            for inner in ast.walk(node):
                if isinstance(inner, ast.Assign):
                    for target in inner.targets:
                        if isinstance(target, ast.Name) and target.id == name:
                            value = ast.literal_eval(inner.value)
                            if isinstance(value, str):
                                return value
    raise PromptDrift(
        f"{name} is not a local string in {module}::{function}. The prompt moved; "
        f"update model_bench.py rather than letting a stale copy be measured.")


def shipping_prompts() -> dict:
    return {
        "scope": _module_string("migrate_client_context.py", "_SCOPE_SYSTEM"),
        "keywords": _module_string("diary_manager.py", "_KEYWORD_EXTRACT_SYSTEM"),
        "people": _module_string("diary_manager.py", "_PEOPLE_EXTRACT_SYSTEM"),
        # The only role that runs on LLM_QUERY_MODEL: `_bind_people_llm` is the
        # single call site left that is not an explicit SCOPE/EXTRACT/MERGE
        # override, so a benchmark without this role never measures the query
        # model at all.
        "people_bind": _local_string("matching_utils.py", "resolve_people_candidates", "system"),
        "search_rewrite": _local_string("fact_manager.py", "rewrite_search_query", "system"),
    }


# ---------------------------------------------------------------------------
# Fixtures
#
# These are written to reproduce the shapes the app actually sees -- mixed
# German/English enterprise material, EPAM-style consulting engagements, the
# document categories that broke the classifier in production -- not to be
# flattering to any model.
# ---------------------------------------------------------------------------
CLIENTS = [
    {"client": "EPAM", "contexts": ["PPC", "Enterprise Architecture", "Data Platform"]},
    {"client": "Deutsche Bank (DB)", "contexts": ["DB AI Adoption"]},
    {"client": "SAP SE", "contexts": []},
    {"client": "Daimler AG (MBAG)", "contexts": []},
]

# The handover entry: names no client and no project, run by EPAM's own
# architects. Correct verdict is EPAM / null / []. Six prompt variants answered
# "PPC" in production before `context_named_in_text()` dropped it in code.
SCOPE_HANDOVER = """## Handover meeting notes

We walked through the current state of the architecture repository. The two
enterprise architects presented the target picture for the next fiscal year,
and the platform team walked through what has been delivered so far.

- Repository structure: the capability map is complete, the application map is
  roughly 70% populated.
- Open question: who owns the integration layer once the platform team hands
  over to operations.
- Decision: the architecture board will review the target picture in the next
  session; no date fixed yet.
- Risk: three of the critical applications still have no owner recorded.

Next steps are the same as last time: finish the application map, then run a
review with the architecture board. The notes from the workshop in September
are still the reference for the capability definitions.
"""

# Generic, no client at all. Correct verdict is null/null -- and a model that
# fills the field anyway is the failure mode that makes a wrong scope permanent.
SCOPE_GENERIC = """## Notes on vector search

Cosine similarity compares the angle between two embeddings and is insensitive
to their magnitude, which is why normalised vectors are usually compared with a
dot product instead. Recall depends on the index: HNSW trades build time and
memory for query speed, while a flat index is exact but linear.

Chunking long documents improves recall for detail questions but duplicates the
payload across chunks, and a query about the middle of a document can then match
the same document several times.
"""

# SAP's own platform: the org's own systems ARE the client, even though SAP is
# also named as a participant's employer. SAP SE has no projects, so the context
# must be null -- the "dead end" the prompt has to say is normal.
SCOPE_SAP = """## SAP RAM/GRC platform review

Reviewed the current state of the RAM and GRC platform with the SAP team. The
platform is SAP's own compliance tooling, and the review covered access
recertification, role mining and the integration with the identity provider.

- Recertification campaigns run quarterly; the last one closed with 2.1% open
  items, which is inside tolerance.
- Role mining is still manual for the finance roles.
- The identity provider integration is scheduled to move to SCIM next year.

Participants were from SAP's own compliance and platform teams.
"""

# The `## Client:` header names the END CUSTOMER, and the stored clients are the
# consulting firms. Echoing the header is the documented failure.
SCOPE_MBAG = """## Client: Daimler AG (MBAG)

Request for information: SAP S/4HANA migration readiness assessment.

Daimler is asking for an assessment of its SAP estate ahead of a possible
migration. Scope would cover the finance and logistics modules, a review of the
custom code inventory, and a rough effort estimate per module.

The engagement would run for eight weeks with two consultants on site. The
question is whether the existing customisations are worth carrying forward or
whether the processes should be re-implemented.
"""

KEYWORD_MEETING = """## Sync with the platform team

We reviewed the open items on the data platform migration. The ingestion
pipeline is behind schedule because the CDC connector for the mainframe keeps
dropping under load. Martin suggested replacing it with a batch export and
accepting a four hour lag; Sandra pushed back because the reporting team needs
the near real time feed.

We agreed to run a two week spike on the connector before deciding anything.

Other topics: the Terraform state file is still in a single bucket, and the
backup policy for the vector database was never defined. The audit in November
will ask for both. Next sync is on the 14th.
"""

KEYWORD_SHORT = """Quick note: the licence renewal for the profiling tool is due at
the end of the quarter, and the budget owner has not confirmed yet.
"""

PEOPLE_HEADING = """## Participants

Alice Smith (host), Bob Jones, Carol Meyer, David Okonkwo, Eva Lindqvist,
Frank Moreau.

## Discussion

The group reviewed the data quality findings from the last quarter. Alice
opened with the summary that 14% of customer records still fail the address
validation rule, down from 31% in the previous quarter. Bob explained that most
of the remaining failures are in the two legacy source systems.

Carol asked whether the fix belongs in the source systems or in the warehouse
transformations. The group agreed that the source systems own it, but that the
warehouse team will document the rule so the business can see it.

David raised the open question of who signs off on the rule change. Eva noted
that the data governance board already approved the rule last year, so the change
is within the existing mandate. Frank will update the ticket and close it.

The next review is in six weeks. Alice will send the updated dashboard before
then.
"""

# Deterministic padding: the documented phi4-mini failure was length-dependent
# (it returned [] for every window of a 6.3k-character entry whose first 100
# characters named six people). A short fixture cannot see that defect.
_PAD_SENTENCES = [
    "The discussion moved to the reporting backlog and the outstanding requests "
    "from the finance team, which are tracked in the same board.",
    "Nothing was decided on the storage tier question; it is carried over to the "
    "next session together with the retention rules.",
    "The vendor assessment is still open and depends on the security review that "
    "has not been scheduled yet.",
    "A short recap of the previous decisions was added to the minutes for anyone "
    "who missed the session.",
]


def long_people_case(chars: int = 6200) -> str:
    body = [PEOPLE_HEADING]
    i = 0
    while sum(len(part) for part in body) < chars:
        body.append(_PAD_SENTENCES[i % len(_PAD_SENTENCES)])
        i += 1
    return "\n\n".join(body)


# The ambiguity the binding hop exists for: two people called John at the same
# client, one of whom the entry is actually about. A wrong bind writes a
# MENTIONS edge, which is a false statement rather than a missed link.
PEOPLE_BIND = {
    "names": ["John"],
    "context": (
        "Kickoff with John on the DB AI Adoption programme. He walked through the "
        "model inventory for credit risk and the governance workstream that extends "
        "the MRM policy with a quarterly attestation."
    ),
    "candidates": [
        {"id": "c-lochas", "name": "John Lochas",
         "text": "John Lochas leads the data platform workstream at Deutsche Bank, "
                 "currently on the DB AI Adoption programme.",
         "score": 1.81, "client": "Deutsche Bank (DB)"},
        {"id": "c-porter", "name": "John Benjamin Porter",
         "text": "John Benjamin Porter is a risk control analyst at Deutsche Bank in "
                 "the markets division, covering traded risk limits.",
         "score": 1.74, "client": "Deutsche Bank (DB)"},
        {"id": "c-meyer", "name": "John Meyer",
         "text": "John Meyer works in procurement at SAP SE and owns the software "
                 "licence renewals.",
         "score": 0.88, "client": "SAP SE"},
    ],
    "expect_ids": ["c-lochas"],
}


REWRITE_CASES = [
    ("People", "who at Deutsche Bank works on the AI adoption programme", None, None),
    ("People", "Thomas Kraikauer", None, None),
    ("", "what did we decide about the ingestion connector", "EPAM", "PPC"),
    ("", "Datenqualität Adressprüfung letztes Quartal", None, None),
]

MERGE_CASES = [
    {
        "id": "db-ai-adoption",
        "records": [
            ("DB AI Adoption — kickoff",
             "Kickoff with Deutsche Bank for the AI adoption programme. Two workstreams: "
             "a model inventory and a governance model for model risk. The inventory "
             "covers 40 models in the credit risk area. Governance follows the bank's "
             "existing MRM policy, extended with a quarterly attestation. Owner: Sandra "
             "Klein. Next step: draft the inventory template by the 14th."),
            ("Deutsche Bank AI Adoption kickoff meeting",
             "First session of the AI adoption engagement at Deutsche Bank. Scope agreed "
             "as two workstreams, a model inventory (40 models, credit risk) and a "
             "governance model that extends the existing MRM policy with quarterly "
             "attestation. Sandra Klein owns the engagement. The inventory template is "
             "due on the 14th; the governance draft comes after that."),
            ("DB AI adoption — notes",
             "Deutsche Bank AI adoption, kickoff. Workstream 1 is the model inventory for "
             "credit risk (40 models). Workstream 2 is governance: MRM policy plus a "
             "quarterly attestation. Sandra Klein is the owner. Deliverable next is the "
             "inventory template, on the 14th."),
        ],
    },
    {
        "id": "vector-db",
        "records": [
            ("Vector database choice",
             "Evaluated Qdrant against pgvector for the memory store. Qdrant wins on "
             "filtered search because payload filters run inside the index, and on "
             "snapshot-based backup. pgvector keeps everything in Postgres, which is one "
             "less store to operate. Decision: Qdrant, with the caveat that the backup "
             "round trip must be tested before go-live."),
            ("Qdrant vs pgvector",
             "Compared Qdrant and pgvector. Filtered search is better in Qdrant (filters "
             "inside the index) and its snapshot API makes backup simpler. The argument "
             "for pgvector is operational simplicity: one store instead of two. Chosen: "
             "Qdrant, conditional on testing the backup round trip first."),
        ],
    },
]

LIVE_CASE = {
    "note_title": "DB AI Adoption — 12 March",
    "note": """# DB AI Adoption — 12 March

## Current state

The model inventory covers 40 models in credit risk. Sandra owns the workstream.
""",
    "segments": [
        "so where we are is that the inventory has forty models in it",
        "all of them in credit risk, and Sandra is running that workstream",
        "the governance piece is separate, it extends the existing MRM policy",
        "and we add a quarterly attestation on top of it",
        "we still need the inventory template, Sandra said the fourteenth",
        "yeah and I was on holiday in Portugal last week which was lovely",
        "the football at the weekend was terrible by the way",
        "so the fourteenth for the template and then governance after that",
        "there is a risk that the MRM policy review slips into April",
        "if it does we should tell the steering committee before the next session",
    ],
}

LIVE_SYSTEM = (
    "You rewrite a live meeting note. You are given the note so far and the "
    "unconsumed transcript segments of a live meeting.\n"
    "Return ONLY a JSON object: {\"title\": \"<short title>\", \"text\": \"<markdown>\"}.\n"
    "RULES:\n"
    "- Summarise; do not transcribe. The note is about the meeting's topic: "
    "decisions, requirements, names, roles, owners, numbers, dates, risks, open "
    "questions.\n"
    "- Leave out small talk, side conversations, unrelated work, and speech that "
    "adds nothing. If a section adds nothing, return the note unchanged.\n"
    "- Never invent a fact. The text is machine speech recognition: no reliable "
    "punctuation, repetitions, and misheard proper nouns and numbers. You may read "
    "a garbled phrase as the thing it sounds like, preferring a spelling that "
    "appears in the note over a homophone. Correcting a misheard word is not "
    "inventing a fact.\n"
    "- Every sentence must carry something a reader would act on. Delete a "
    "sentence that only restates another, and never add commentary about how hard "
    "the speech was to follow.\n"
    "- Return valid Markdown, under 12000 characters."
)

MERGE_SYSTEM = (
    "You merge duplicate memory records into one. You are given the source "
    "records, which describe the same thing.\n"
    "Return ONLY a JSON object: {\"title\": \"<title>\", \"text\": \"<merged text>\"}.\n"
    "RULES: keep every fact that appears in any source record -- numbers, names, "
    "dates, owners, decisions, open questions. Drop only what is duplicated. Do "
    "not add a fact that is not in a source record. Write one coherent record, not "
    "a list of the sources."
)


# ---------------------------------------------------------------------------
# Scoring helpers (objective, boring, stable)
# ---------------------------------------------------------------------------
_WORD = re.compile(r"[0-9A-Za-zÄÖÜäöüß]+")


def tokens(text: str) -> set:
    return {w.casefold() for w in _WORD.findall(text or "") if len(w) > 3}


def retention(draft: str, source: str) -> float:
    """Share of the source's content words that survived into the draft.

    This is the property that killed granite on the merge role (90.7/5.1/9.3/14.6
    against nemotron's 100/99.1/100/98.8): a merge that reads well but drops a
    record is worse than one that reads badly.
    """
    src = tokens(source)
    if not src:
        return 1.0
    return round(len(src & tokens(draft)) / len(src), 3)


_TOKEN_RUN = re.compile(r"(\S+)(?:\s+\1\b){3,}")


def degenerate(text: str) -> str:
    """'' when the answer is usable, else a short reason.

    Both shapes matter and they are different: an empty answer is a model that
    produced nothing, a repeated run is a decoder locked into a loop. The second
    is not empty and reads as valid text, which is why every layer above it used
    to treat it as "the model found nothing".
    """
    stripped = (text or "").strip()
    if not stripped:
        return "empty"
    words = stripped.split()
    if len(words) >= 8:
        counts = {}
        for word in words:
            counts[word] = counts.get(word, 0) + 1
        share = max(counts.values()) / len(words)
        if share >= 0.6:
            return f"run of one token ({share:.0%} of {len(words)} words)"
    if _TOKEN_RUN.search(stripped):
        return "repeated phrase"
    return ""


def json_object(text: str):
    m = re.search(r"\{.*\}", text or "", re.DOTALL)
    if not m:
        return None, "no JSON object"
    try:
        return json.loads(m.group()), ""
    except json.JSONDecodeError as exc:
        return None, f"invalid JSON: {exc}"


def json_array(text: str):
    m = re.search(r"\[.*\]", text or "", re.DOTALL)
    if not m:
        return None, "no JSON array"
    try:
        return json.loads(m.group()), ""
    except json.JSONDecodeError as exc:
        return None, f"invalid JSON: {exc}"


def resolve_name(answer, names) -> str:
    """Map an answer onto a stored name, mirroring `resolve_scope_name`'s ladder.

    Exact -> token subset -> containment -> fuzzy ratio. Deliberately not
    importing the real resolver: this scores the answer, and a scorer that can
    fail is one that reports a defect in the benchmark instead of the model.
    """
    if not answer or not isinstance(answer, str):
        return ""
    import difflib

    lowered = {name.casefold(): name for name in names}
    key = answer.strip().casefold()
    if key in lowered:
        return lowered[key]
    answer_tokens = set(_WORD.findall(key))
    best, best_ratio = "", 0.0
    for stored in names:
        stored_tokens = set(_WORD.findall(stored.casefold()))
        if not stored_tokens:
            continue
        if stored_tokens <= answer_tokens or answer_tokens <= stored_tokens:
            return stored
        if stored.casefold() in key or key in stored.casefold():
            return stored
        ratio = difflib.SequenceMatcher(None, key, stored.casefold()).ratio()
        if ratio > best_ratio:
            best, best_ratio = stored, ratio
    return best if best_ratio >= 0.75 else ""


_FILTER_ECHO = re.compile(r"categor|client|context|scope", re.I)


def filter_echo(keyword: str) -> bool:
    """The rewriter echoing the SEARCH FILTERS line back as content.

    Measured at 69 of 469 rewrites in production, and cached for the TTL, so it
    was served repeatedly. A stem match on the filter *keys*, not a token-subset
    test: a subset test would also drop "bank" under client=Deutsche Bank.
    """
    return bool(_FILTER_ECHO.search(keyword or ""))


# ---------------------------------------------------------------------------
# Cases
# ---------------------------------------------------------------------------
def build_cases(prompts: dict, long_people: bool = True) -> dict:
    clients_json = "[" + ", ".join(
        json.dumps({"client": c["client"], "contexts": c["contexts"]}, ensure_ascii=False)
        for c in CLIENTS) + "]"

    def scope_case(case_id, item, tags="", expect=None):
        prompt = f"KNOWN CLIENTS:\n{clients_json}\n\nITEM:\n{item}"
        if tags:
            prompt = f"KNOWN CLIENTS:\n{clients_json}\n\n{tags}\n\nITEM:\n{item}"
        return {
            "id": case_id, "system": prompts["scope"], "prompt": prompt,
            "num_predict": 80, "expect": expect or {},
        }

    people_text = long_people_case() if long_people else PEOPLE_HEADING

    return {
        "scope": [
            scope_case("handover-epam", SCOPE_HANDOVER,
                       tags="MENTIONS:\n- [own client: EPAM] Enterprise Architect",
                       expect={"client": "EPAM", "context": None, "related": [],
                               "why": "no client or project named; EPAM's own architects"}),
            scope_case("generic-vector", SCOPE_GENERIC,
                       expect={"client": None, "context": None, "related": [],
                               "why": "generic knowledge; a guess is stamped forever"}),
            scope_case("sap-own-platform", SCOPE_SAP,
                       tags="MENTIONS:\n- [own client: SAP SE] Compliance Lead",
                       expect={"client": "SAP SE", "context": None, "related": [],
                               "why": "SAP's own systems; SAP SE has no projects"}),
            scope_case("mbag-rfi", SCOPE_MBAG,
                       expect={"client": "Daimler AG (MBAG)", "context": None,
                               "related": [], "why": "header names the end customer"}),
        ],
        "keywords": [
            {"id": "platform-sync", "system": prompts["keywords"],
             "prompt": f"ENTRY: Data platform migration sync\n\n{KEYWORD_MEETING}",
             "num_predict": 200, "expect": {"source": KEYWORD_MEETING}},
            {"id": "short-note", "system": prompts["keywords"],
             "prompt": f"ENTRY: Licence renewal\n\n{KEYWORD_SHORT}",
             "num_predict": 200, "expect": {"source": KEYWORD_SHORT}},
        ],
        "people": [
            {"id": "participants-long", "system": prompts["people"], "prompt": people_text,
             "num_predict": 400,
             "expect": {"names": ["Alice Smith", "Bob Jones", "Carol Meyer",
                                  "David Okonkwo", "Eva Lindqvist", "Frank Moreau"]}},
        ],
        "people_bind": [
            {"id": "two-johns", "system": prompts["people_bind"],
             "prompt": (f"EXTRACTED NAMES: {json.dumps(PEOPLE_BIND['names'], ensure_ascii=True)}\n"
                        f"DIARY CONTEXT: {PEOPLE_BIND['context']}\n"
                        f"CANDIDATES: {json.dumps(PEOPLE_BIND['candidates'], ensure_ascii=True)}"),
             "num_predict": 300,
             "expect": {"ids": PEOPLE_BIND["expect_ids"],
                        "why": "John Lochas runs the AI adoption workstream; Porter is "
                               "markets risk, Meyer is SAP procurement"}},
        ],
        "search_rewrite": [
            {"id": f"rewrite-{i}", "system": prompts["search_rewrite"],
             "prompt": (f"SEARCH FILTERS: {filters or 'none'}\nQUERY: {query}"),
             "num_predict": 200, "expect": {"query": query}}
            for i, (category, query, client, context) in enumerate(REWRITE_CASES)
            for filters in [", ".join(v for v in (
                f"category={category}" if category else "",
                f"client={client}" if client else "",
                f"context={context}" if context else "") if v)]
        ],
        "merge": [
            {"id": case["id"], "system": MERGE_SYSTEM,
             "prompt": "SOURCE RECORDS:\n" + "\n\n".join(
                 f"[{i + 1}] {title}\n{text}" for i, (title, text) in enumerate(case["records"])),
             "num_predict": 1200, "expect": {"records": case["records"]}}
            for case in MERGE_CASES
        ],
        "live": [
            {"id": "db-ai-adoption", "system": LIVE_SYSTEM,
             "prompt": (f"EXISTING NOTE:\n{LIVE_CASE['note']}\n\n"
                        f"UNCUNSUMED SEGMENTS:\n" +
                        "\n".join(f"[{i * 4:02d}s] Speaker {(i % 2) + 1}: {s}"
                                  for i, s in enumerate(LIVE_CASE["segments"]))),
             "num_predict": 800,
             "expect": {"smalltalk": ["Portugal", "football", "holiday"]}},
        ],
    }


# ---------------------------------------------------------------------------
# Running
# ---------------------------------------------------------------------------
def score(role: str, case: dict, answer: str) -> dict:
    """Objective checks only. Everything a human must judge stays in `answer`."""
    out = {"degenerate": degenerate(answer)}
    expect = case.get("expect") or {}

    if role == "scope":
        data, err = json_object(answer)
        if data is None:
            out["json"] = err
            return out
        out["json"] = "ok"
        stored = [c["client"] for c in CLIENTS]
        got_client = resolve_name(data.get("client"), stored)
        contexts = {c["client"]: c["contexts"] for c in CLIENTS}
        got_context = ""
        if got_client:
            got_context = resolve_name(data.get("context"), contexts.get(got_client, []))
        out["client_raw"] = data.get("client")
        out["context_raw"] = data.get("context")
        out["client_resolved"] = got_client
        out["context_resolved"] = got_context
        if "client" in expect:
            want_client = expect["client"]
            out["client_ok"] = (got_client or "") == (want_client or "")
            want_context = expect.get("context")
            out["context_ok"] = (got_context or "") == (want_context or "")
            out["why"] = expect.get("why", "")
        else:
            out["client_ok"] = None
            out["context_ok"] = None
        return out

    if role == "keywords":
        data, err = json_object(answer)
        if data is not None:
            words = data.get("keywords")
        else:
            words, err2 = json_array(answer)
            err = err if words is not None else f"{err} / {err2}"
            if words is None:
                out["json"] = err
                return out
        out["json"] = "ok"
        if not isinstance(words, list):
            out["json"] = "keywords is not a list"
            return out
        cleaned = [w for w in words if isinstance(w, str) and w.strip()]
        out["count"] = len(cleaned)
        source = tokens(expect.get("source", ""))
        invented = [w for w in cleaned if not (tokens(w) & source)]
        out["invented"] = invented
        out["invented_share"] = round(len(invented) / len(cleaned), 2) if cleaned else 0.0
        out["too_long"] = [w for w in cleaned if len(w.split()) > 3]
        return out

    if role == "people":
        data, err = json_array(answer)
        if data is None:
            out["json"] = err
            return out
        out["json"] = "ok"
        found = [n for n in data if isinstance(n, str)]
        out["found"] = found
        want = expect.get("names", [])
        matched = [w for w in want if resolve_name(w, found) or any(
            resolve_name(f, [w]) for f in found)]
        out["recall"] = f"{len(matched)}/{len(want)}"
        out["missed"] = [w for w in want if w not in matched]
        out["hallucinated"] = [f for f in found if resolve_name(f, want) == "" and
                               not any(resolve_name(f, [m]) for m in matched)]
        out["speaker_labels"] = [f for f in found if re.search(r"SPEAKER\s*\d", f, re.I)]
        out["parenthetical"] = [f for f in found if "(" in f]
        return out

    if role == "people_bind":
        data, err = json_object(answer)
        if data is None:
            out["json"] = err
            return out
        out["json"] = "ok"
        want = expect.get("ids", [])
        known = {c["id"] for c in PEOPLE_BIND["candidates"]}
        selected = data.get("matches")
        if not isinstance(selected, list):
            out["json"] = "matches is not a list"
            return out
        # The acceptance rule is copied from resolve_people_candidates on
        # purpose: `bool` is an int subclass, so `confidence >= 0.8` accepts
        # True unless it is excluded, and a bind the app rejects is not a bind.
        accepted = []
        for selection in selected:
            if not isinstance(selection, dict):
                continue
            fact_id = str(selection.get("fact_id") or "")
            confidence = selection.get("confidence")
            if (fact_id in known and isinstance(confidence, (int, float))
                    and not isinstance(confidence, bool) and confidence >= 0.8
                    and fact_id not in accepted):
                accepted.append(fact_id)
        out["accepted"] = accepted
        out["correct"] = sorted(accepted) == sorted(want)
        out["wrong_binds"] = [i for i in accepted if i not in want]
        out["invented_ids"] = [i for i in accepted if i not in known]
        out["why"] = expect.get("why", "")
        return out

    if role == "search_rewrite":
        data, err = json_object(answer)
        if data is None:
            out["json"] = err
            return out
        out["json"] = "ok"
        words = [w for w in data.get("keywords", []) if isinstance(w, str)]
        out["keywords"] = words
        out["count"] = len(words)
        out["filter_echo"] = [w for w in words if filter_echo(w)]
        out["too_long"] = [w for w in words if len(w.split()) > 3]
        out["echoes_query"] = [w for w in words
                               if w.strip().casefold() == (expect.get("query") or "").casefold()]
        return out

    if role == "merge":
        data, err = json_object(answer)
        if data is None:
            out["json"] = err
            return out
        out["json"] = "ok"
        text = data.get("text") or ""
        out["title"] = (data.get("title") or "")[:120]
        out["chars"] = len(text)
        records = expect.get("records", [])
        out["retention"] = [retention(text, body) for _, body in records]
        out["retention_min"] = min(out["retention"]) if out["retention"] else 0.0
        return out

    if role == "live":
        data, err = json_object(answer)
        if data is None:
            out["json"] = err
            return out
        out["json"] = "ok"
        text = (data.get("text") or "") + "\n" + (data.get("title") or "")
        out["chars"] = len(text)
        out["smalltalk"] = [w for w in expect.get("smalltalk", [])
                            if re.search(re.escape(w), text, re.I)]
        out["headings"] = len(re.findall(r"^#{1,3} ", text, re.M))
        return out

    return out


def run(base_url: str, models: list, cases: dict, *, think_modes: list,
        timeout: float, samples: int, roles: list,
        num_ctx: int = DEFAULT_NUM_CTX, log=print) -> dict:
    results = {"url": base_url, "started": time.strftime("%Y-%m-%d %H:%M:%S"),
               "num_ctx": num_ctx, "runs": [], "thinking_unsupported": []}
    unsupported = set()
    for model in models:
        for think in think_modes:
            # A model without a thinking mode answers the first `think: true`
            # call with a 400 and would answer every later one the same way, so
            # the rest of that mode is skipped instead of burning a call per
            # role to relearn it.
            if think and model in unsupported:
                continue
            for role in roles:
                # The role carries its own sampling (production's repeat penalty),
                # so the options are resolved per role rather than per model.
                options = sampling_for(model, think, role)
                selected = cases[role][:samples]
                for case in selected:
                    label = f"{model} | think={think} | {role} | {case['id']}"
                    log(f"  {label} ...", )
                    # A thinking model spends the budget in the reasoning block
                    # first, so the production budget starves it and the content
                    # comes back empty. The bonus applies to think mode only.
                    budget = case.get("num_predict", 0)
                    if budget and think:
                        budget += THINK_BUDGET_BONUS
                    try:
                        answer = chat(
                            base_url, model, case["system"], case["prompt"],
                            think=think, options=options,
                            num_predict=budget,
                            num_ctx=num_ctx, timeout=timeout)
                    except OllamaError as exc:
                        if think and "does not support thinking" in str(exc).lower():
                            log(f"\r  {label} -> thinking unsupported")
                            results["thinking_unsupported"].append(model)
                            unsupported.add(model)
                            break
                        log(f"\r  {label} -> FAILED {exc}")
                        results["runs"].append({
                            "model": model, "think": think, "role": role,
                            "case": case["id"], "options": options,
                            "error": str(exc), "answer": "", "score": {},
                        })
                        continue
                    scored = score(role, case, answer["content"])
                    scored["degenerate"] = scored.get("degenerate") or (
                        degenerate(answer["thinking"]) if answer["thinking"] else "")
                    results["runs"].append({
                        "model": model, "think": think, "role": role,
                        "case": case["id"], "options": options,
                        "num_predict": budget,
                        "expect": case.get("expect", {}),
                        "seconds": answer["seconds"],
                        "prompt_tokens": answer["prompt_tokens"],
                        "answer_tokens": answer["answer_tokens"],
                        "done_reason": answer["done_reason"],
                        "thinking_chars": len(answer["thinking"]),
                        "answer": answer["content"],
                        "score": scored,
                    })
                    log(f"\r  {label} -> {answer['seconds']}s "
                        f"{len(answer['content'])} chars")
    results["finished"] = time.strftime("%Y-%m-%d %H:%M:%S")
    return results


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------
def _cell(value) -> str:
    return str(value).replace("|", "/").replace("\n", " ")


def summarise(results: dict) -> dict:
    table = {}
    for run in results["runs"]:
        key = (run["model"], run["think"], run["role"])
        bucket = table.setdefault(key, {"n": 0, "errors": 0, "degen": 0,
                                        "seconds": [], "score": {}})
        bucket["n"] += 1
        if run.get("error"):
            bucket["errors"] += 1
            continue
        if run["score"].get("degenerate"):
            bucket["degen"] += 1
        bucket["seconds"].append(run.get("seconds") or 0)
        for name, value in run["score"].items():
            if isinstance(value, bool):
                bucket["score"][name] = bucket["score"].get(name, 0) + int(value)
            elif name == "retention":
                bucket["score"].setdefault("retention", []).extend(value)
            elif name == "invented_share":
                bucket["score"].setdefault("invented_share", []).append(value)
            elif name == "count":
                bucket["score"].setdefault("count", []).append(value)
    return table


def to_markdown(results: dict) -> str:
    lines = ["# Model benchmark", "",
             f"- url: `{results['url']}`",
             f"- num_ctx: {results.get('num_ctx', 'model default')}",
             f"- window: {results.get('started')} -> {results.get('finished')}", ""]
    if results.get("thinking_unsupported"):
        lines += [f"- thinking not supported by: {', '.join(sorted(set(results['thinking_unsupported'])))}", ""]

    lines += ["## Summary", "",
              "| model | think | role | n | err | degenerate | s (mean) | objective |",
              "|---|---|---|---|---|---|---|---|"]
    for (model, think, role), bucket in sorted(summarise(results).items()):
        bits = []
        for name, value in sorted(bucket["score"].items()):
            if name == "retention":
                bits.append(f"retention min={min(value):.2f} mean={statistics.mean(value):.2f}")
            elif name == "invented_share":
                bits.append(f"invented={statistics.mean(value):.0%}")
            elif name == "count":
                bits.append(f"count mean={statistics.mean(value):.1f}")
            else:
                bits.append(f"{name}={value}")
        lines.append("| {} | {} | {} | {} | {} | {} | {:.1f} | {} |".format(
            _cell(model), think, role, bucket["n"], bucket["errors"], bucket["degen"],
            statistics.mean(bucket["seconds"]) if bucket["seconds"] else 0.0,
            _cell("; ".join(bits))))
    lines.append("")

    lines += ["## Raw outputs", "",
              "Every answer is printed in full: the judgement that matters (is this "
              "scope verdict right, is this note about the meeting) is not something "
              "a regex can make.", ""]
    for run in results["runs"]:
        head = f"### {run['model']} · think={run['think']} · {run['role']} · {run['case']}"
        lines += [head, ""]
        if run.get("error"):
            lines += [f"**FAILED**: `{run['error']}`", ""]
            continue
        meta = (f"options={json.dumps(run['options'])} · {run['seconds']}s · "
                f"prompt={run['prompt_tokens']} tok · answer={run['answer_tokens']} tok · "
                f"done={run['done_reason']} · thinking={run['thinking_chars']} chars")
        lines += [meta, ""]
        scored = {k: v for k, v in run["score"].items() if k not in ("why",)}
        if run.get("expect"):
            scored["expected"] = run["expect"]
        lines += ["```json", json.dumps(scored, ensure_ascii=False, indent=1)[:2000], "```", ""]
        lines += ["```", run["answer"][:4000], "```", ""]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Stub backend (self-test, and what makes this runnable with no Ollama)
# ---------------------------------------------------------------------------
def _stub_answer(role: str, case_id: str) -> str:
    return {
        "scope": '{"client": "EPAM", "context": null, "related": []}',
        "keywords": '{"keywords": ["ingestion pipeline", "cdc connector", "terraform", '
                    ' "backup policy", "audit"]}',
        "people": '["Alice Smith", "Bob Jones", "Carol Meyer", "David Okonkwo", '
                  '"Eva Lindqvist", "SPEAKER1", "Frank Moreau (PM)"]',
        "people_bind": '{"matches": [{"fact_id": "c-lochas", "confidence": 0.92}]}',
        "search_rewrite": '{"keywords": ["People category", "deutsche bank ai adoption",'
                          ' "ai adoption programme"]}',
        "merge": '{"title": "DB AI Adoption kickoff", "text": "Kickoff with Deutsche Bank. '
                 'Two workstreams. Sandra Klein owns it."}',
        "live": '{"title": "DB AI Adoption", "text": "# DB AI Adoption\\n\\n'
                'Inventory covers 40 credit-risk models. Sandra owns it."}',
    }.get(role, "")


def run_stub(cases: dict, roles: list, samples: int, log=print) -> dict:
    results = {"url": "stub://", "started": time.strftime("%Y-%m-%d %H:%M:%S"),
               "runs": [], "thinking_unsupported": [], "stub": True}
    for model in ("stub-model:2b",):
        for think in (False, True):
            for role in roles:
                for case in cases[role][:samples]:
                    content = _stub_answer(role, case["id"])
                    scored = score(role, case, content)
                    results["runs"].append({
                        "model": model, "think": think, "role": role,
                        "case": case["id"], "options": sampling_for(model, think, role),
                        "expect": case.get("expect", {}),
                        "seconds": 0.0, "prompt_tokens": 0, "answer_tokens": 0,
                        "done_reason": "stub", "thinking_chars": 0,
                        "answer": content, "score": scored,
                    })
    results["finished"] = time.strftime("%Y-%m-%d %H:%M:%S")
    return results


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--url", default=DEFAULT_URL)
    parser.add_argument("--models", nargs="*", default=None,
                        help="models to test (default: everything /api/tags lists, "
                             f"minus {EMBEDDER_HINT})")
    parser.add_argument("--roles", nargs="*", default=list(ROLES), choices=list(ROLES))
    parser.add_argument("--samples", type=int, default=99,
                        help="cases per role (default: all of them)")
    parser.add_argument("--think", choices=("both", "on", "off"), default="both")
    parser.add_argument("--num-ctx", type=int, default=DEFAULT_NUM_CTX,
                        help="context window sent to every model (default: $OLLAMA_CONTEXT_LENGTH "
                             "or 16332) -- without it each tag uses its own default, which is what "
                             "made the first run an unequal comparison")
    parser.add_argument("--timeout", type=float, default=300.0)
    parser.add_argument("--out", default="", help="directory for bench.json/bench.md")
    parser.add_argument("--dry-run", action="store_true", help="print the plan, no network")
    parser.add_argument("--stub", action="store_true", help="self-test against canned answers")
    parser.add_argument("--no-long-people", action="store_true",
                        help="use the short people fixture instead of the ~6k one")
    args = parser.parse_args(argv)

    try:
        prompts = shipping_prompts()
    except PromptDrift as exc:
        print(f"prompt lift failed: {exc}", file=sys.stderr)
        return 2

    cases = build_cases(prompts, long_people=not args.no_long_people)
    think_modes = {"both": [False, True], "on": [True], "off": [False]}[args.think]

    if args.dry_run:
        print(f"url:   {args.url}")
        print(f"roles: {', '.join(args.roles)}")
        print(f"think: {think_modes}")
        print(f"num_ctx: {args.num_ctx}")
        for role in args.roles:
            for case in cases[role][:args.samples]:
                print(f"  {role:15s} {case['id']:20s} "
                      f"system={len(case['system']):5d} prompt={len(case['prompt']):5d} "
                      f"num_predict={case.get('num_predict', 0)}")
        return 0

    if args.stub:
        results = run_stub(cases, args.roles, args.samples)
    else:
        try:
            models = args.models or list_models(args.url)
        except OllamaError as exc:
            print(f"cannot reach {args.url}: {exc}", file=sys.stderr)
            print("Run this where Ollama lives, or pass --url.", file=sys.stderr)
            return 3
        if not models:
            print("no chat models found (only the embedder?)", file=sys.stderr)
            return 3
        print(f"models: {', '.join(models)}")
        print(f"plan:   {len(models)} models x {len(think_modes)} modes x "
              f"{sum(len(cases[r][:args.samples]) for r in args.roles)} cases "
              f"at num_ctx={args.num_ctx}")
        results = run(args.url, models, cases, think_modes=think_modes,
                      timeout=args.timeout, samples=args.samples, roles=args.roles,
                      num_ctx=args.num_ctx)

    markdown = to_markdown(results)
    if args.out:
        os.makedirs(args.out, exist_ok=True)
        with open(os.path.join(args.out, "bench.json"), "w", encoding="utf-8") as handle:
            json.dump(results, handle, ensure_ascii=False, indent=1)
        path = os.path.join(args.out, "bench.md")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(markdown)
        print(f"wrote {path} and bench.json")
    else:
        print(markdown)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
