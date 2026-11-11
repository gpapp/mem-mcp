---
name: client-context-hygiene
description: How to audit which records are filed under which client and project — in what order to check, what "clean" looks like, and which fixes you must not attempt from an agent session because they make the vault permanently worse.
---

# Skill: Client & Project Hygiene

## When to Use This Skill

When the vault answers "we have nothing on this client" and you believe that is
wrong. When a client panel looks empty but a search finds plenty. Before a bulk
reclassification. Or on a regular cadence, because scope decays slowly and
silently: every record written while no client existed stays unscoped forever
unless somebody looks.

This skill **reads and reports**. It does not repoint records, and the section
"What you must not do from here" explains why that is not modesty.

## What Scope Actually Is

Scope is a pair of **edges**, not a field:

- `(:Fact)-[:FOR_CLIENT]->(:Client)` — the engagement this record is about.
- `(:Fact)-[:IN_CONTEXT]->(:Context)` — the project under that client.
- `(:Fact)-[:RELEVANT_TO]->(:Client|:Context)` — this record also discusses it.
- Diary entries carry the same three.

There is no `clientId` property on any `Fact` or `DiaryEntry` node. That key
exists only in the Qdrant payload. Reading a `clientId` property off a node
answers `None` for everything and makes the whole vault look unclassified — a
mistake that has already been made once in this codebase, in graph scope
filtering, and it fails silently.

A record is **visible under a scope** when any one of those edges points at it.
A record filed under the wrong client is not merely missing from the right one;
it actively appears under the wrong one.

## The Review Order

Each step narrows the next. Do them in this order.

1. **List the clients, with their projects.** `list_clients`. You need the exact
   stored spellings — `Deutsche Bank (DB)`, not `DB` — because every later step
   matches against them. Note which clients have no projects: that is normal,
   not a defect, and a client with no contexts is a perfectly valid scope.

2. **Find what claims no scope at all.** Search a subject you know exists and
   read the scope back from the results: `search_facts("...", limit=10)`. A
   record that comes back with no client is unscoped. Cross-check the client
   panel's own item list rather than trusting a single query — a record can be
   missing from a panel because it is unscoped, or because it is filed under a
   project while you are looking at the client, or because it only *mentions*
   the engagement through `RELEVANT_TO`.

3. **Look for scopes that should not exist.** `list_clients` returning two
   entries whose names differ only by a suffix (`EPAM` and `EPAM Systems`) is
   the signature of a near-miss having been created. Two clients with the same
   people and different records is the state this skill exists to prevent, not
   to repair — see below.

4. **Reclassify, then verify.** Reclassification is a **dashboard action**
   (`POST /api/maintenance/reclassify`, or the per-record menu in Memories /
   Diary), not an MCP tool. After it runs, repeat step 2 on the same queries.
   There is no point reclassifying twice in a row: the second run sees the same
   evidence and produces the same answers, and the verdict is stamped.

## What "Clean" Looks Like

Not "every record has a client". A null scope is a **real, correct, expected**
answer — the vault is allowed to contain a note about a topic that belongs to no
engagement. Clean means:

- the records that clearly belong to an engagement are filed under the **exact
  stored name** of that client or project;
- no two clients share a name modulo a suffix;
- the records that legitimately belong to nothing are null, not guessed into the
  nearest client.

A guessed scope is worse than a null. A null is recoverable — somebody
reclassifies it later. A wrong scope is **permanent**: the verdict is stamped
with `scopeCheckedSig` and no later run revisits it, so the record answers the
wrong client's queries forever, and the reader has no way to tell.

## What You Must Not Do From Here

Each of these looks like a fix and makes the vault worse.

- **Do not create a client on a near miss.** `create_client("EPAM Systems")`
  when `EPAM` already exists mints a second node. Both match a client filter,
  the counts disagree, and neither is the node other queries resolve to. The
  duplicate is permanent, because the classifier only ever chooses from existing
  names — so the near-miss copy is never linked and never gets a second look.
  If a name is wrong, say so and let a human rename the existing client.

- **Do not repoint a record with `update_fact`.** That tool takes a name, text
  and category, and deliberately cannot change scope. It says so in its own
  description. The remedy for "this belongs under a different client" is the
  dashboard's reclassify, or a human editing the scope — not a text edit that
  leaves the edges where they were.

- **Do not clear a hand-set scope.** A record a human filed by hand carries
  `scopeManual`. A **bulk** reclassify skips those, which is the marker's whole
  purpose. A **single-item** reclassify does *not* skip them — that is an
  explicit request for one record, and it clears the marker first. So a
  single-item reclassify on a hand-filed record is a deliberate override; do not
  do it to tidy a list.

- **Do not diagnose from an empty result alone.** An empty search is four
  different problems wearing one face: the vault genuinely has nothing, the
  scope filter named a client that does not resolve, the record is filed under a
  sibling project, or the query is too narrow. Widen before concluding.

## Reporting

When you finish, say four things: how many clients exist and which are inactive,
which subjects you checked and what came back unscoped, whether you saw any
duplicate-looking client names, and what still needs a human. Never report "the
vault is clean" — report what you checked. The step nobody ran is the one that
was wrong.
