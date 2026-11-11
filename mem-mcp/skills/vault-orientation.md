---
name: vault-orientation
description: The first-hour guide to this vault — what the three kinds of record are, how clients and projects differ from categories, how scope attaches, how searching actually behaves, and the handful of questions worth trying first.
---

# Skill: Vault Orientation

## When to Use This Skill

At the start of a session with an unfamiliar vault, or when you are about to
explain this product to somebody. It is deliberately the shortest path to being
useful: read it once, and the rest of the skills make sense.

## The Three Kinds of Record

| | what it is | where it lives |
|---|---|---|
| **Fact** | a statement about the world — a person, a tool, a decision context, a risk | Neo4j `:Fact`, embedded in Qdrant |
| **Diary entry** | what happened in one meeting, in Markdown | Neo4j `:DiaryEntry`, embedded |
| **Live note** | a growing summary of one recording session | **a Fact**, category `Notes` |

Two consequences worth internalising:

- **A live note is an ordinary fact.** It is searchable, it appears in Memories
  and on the graph, and it is in every savepoint. There is no separate note
  system to learn.
- **The raw transcript of a recording is not searchable and is not a fact.** It
  is Neo4j-only evidence behind the note. A search for a phrase returns the
  note, never the transcript. If you want the transcript, ask for the
  transcription, not for a search.

Diary entries are where meetings live, and they are longer and messier than
facts: they may contain `## Decisions`, `## Actions` and `## Open Questions`
headings that the vault parses into a decision log and a commitments register.
Facts are extracted statements, one idea each.

## Clients and Projects, Not Folders

- **Client** = the organisation whose engagement you are working on. This is the
  organisation the work is *for*, which is not always the organisation named
  loudest in the text: a document whose header says a bank's name may be a
  consultancy's own engagement, and the vault's clients are the consultancy.
- **Context** (called a *project* in the UI) = one piece of work under one
  client. Projects are parents-children: a context always belongs to a client.

Scope attaches as **edges** — `FOR_CLIENT`, `IN_CONTEXT`, and `RELEVANT_TO` for
"this record also discusses it". A record may be linked to several
`RELEVANT_TO` targets, and a record with no primary client may still be findable
because one of those edges points at the engagement.

There is no scope *field* on a record. If you are writing Cypher against this
vault, read the edges; a `clientId` property does not exist on `Fact` or
`DiaryEntry`, and reading it answers `None` for everything.

## Categories Are Global

Categories (`People`, `Technology`, `Tool`, `Notes`, …) are shared across the
whole vault and belong to nobody. They are not folders, not a second scoping
system, and they carry no owner. Use them to narrow a search; never to reason
about who a record belongs to.

## Searching Behaves in Three Ways People Get Wrong

1. **A client or project filter ranks and never excludes.** Asked for one
   client, the vault returns that client's records first, then everything else
   ranked lower. It does not filter. So "no results for this client" almost
   always means the query was wrong, not that the vault is empty — and a record
   from another engagement still coming back is the feature working.
2. **A date window or a person *does* narrow.** `after`, `before` and `person`
   remove records, because naming a window or a person states which records are
   in question. A record outside the window is a wrong answer rather than a
   lower-ranked one. These are the opposite of scope and the docstrings say so.
3. **Look at `confidence` and `evidence`, not just the ranking.** `score` orders
   results; it is not a similarity and thresholding it throws away good hits.
   `confidence` is what the `top_p` gate reads. `evidence` names why a hit was
   kept — `exact`, `alias`, `first+last`, `first_name` — or why it is a near
   miss: `surname_strong`, `fuzzy_name`, `name_conflict`. Anything under
   "unconfirmed matches" is a warning, not a result set, and must not be used to
   assert who somebody is.

A long record is stored as several vectors but returned as one record. Two hits
sharing an id are one entry, not a duplicate.

## The First Questions to Try

1. `list_clients` — who does this vault know about, and which of them are still
   active?
2. `search_facts("...", client="<name>")` — what is recorded about one
   engagement?
3. `list_diary_entries` — what meetings exist, newest first?
4. `list_action_items` — what is owed, by whom, and what is overdue?
5. `client_brief("<client>")` — one page: where it stands, what is open, what is
   known, and what is missing.
6. `client_timeline("<client>")` — the same engagement as a story, month by
   month.

If the answer to (2) is empty, run it again without the client and read the
scope off the results. That is the test for "the vault has nothing" versus "the
name did not resolve".

## What This Vault Is Not

- **It is not a calendar.** Dates exist on records and on commitments; there is
  no scheduling, no reminders, no external writes.
- **It is not a task manager shared across people.** Commitments belong to a
  vault owner. Sharing exposes a client or project read-only or read-write; it
  does not assign work between accounts.
- **It is not a full-text index of what was said.** It stores summaries and
  extracted statements. The one exception is the diary entry itself, which is
  stored in full.

## A Good First Session

List the clients, read one brief, and read that engagement's open commitments.
Three calls, and you can hold a conversation about where the work stands. Doing
that before searching is what stops an agent from answering a scoping question
with a keyword guess.
