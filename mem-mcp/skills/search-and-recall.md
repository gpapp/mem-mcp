---
name: search-and-recall
description: How to read this vault well — which search tool answers which question, how scope and confidence actually behave, and what each result field is telling you before you quote it or write anything based on it.
---

# Skill: Search and Recall

## When to Use This Skill

Use it whenever you are about to answer from memory rather than from a document:
who somebody is, what was agreed with a client, what was said in a meeting, what
the vault already knows about a system. It is a **read** workflow. It exists
because the three search tools look interchangeable and are not, and because
every result carries fields that decide whether you may act on it.

## The Three Tools, and What Each Is For

| you want | tool |
|---|---|
| a fact — a person, a technology, a tool, a client, a project | `search_facts` |
| a meeting — what was discussed, decided, promised | `diary_search_entries` |
| what keeps recurring across all of it | `find_patterns` |

None of them is a search engine over prose. Facts are short, named statements.
Diary entries are meeting write-ups keyed by date. Choose by *what kind of thing
you are looking for*, not by which tool sounds more general.

`find_patterns` is the one that gets misused. It answers **"what recurs across
this vault"** — repeated themes, strengths, clusters. It does **not** answer
"what is true of X". Asking it about a person or a project returns the ambient
shape of the vault and invites you to present that as a finding. If you want a
statement about a particular thing, search for that thing.

## Query the Index, Not the Conversation

Both `search_facts` and `diary_search_entries` match **keywords that appear in
stored text**. Write queries as terms, not sentences.

- Good: `Alice`, `SAP`, `DB AI Adoption`, `migration`, `2026-07-15`, `May 2026`
- Bad: `who do I work with on AI projects`, `things I need to follow up on`,
  `what am I passionate about`

The bad form is not a style preference. A query with no lexical overlap with any
stored record returns nothing useful, and nothing in the response says the
question was unanswerable — it looks like an empty vault.

Search short. If a long query returns weak results, **shorten it** rather than
rephrasing: a person's first name alone, a project's name alone, a single
technology. Long queries are rewritten by a small model before the vector
search; short ones (two words or fewer) go straight to the index.

Repeating the same query with the same filters inside a session is free — the
rewrite is cached — so refine by changing the terms, not by retrying the same
ones.

## Always Pass the Scope You Already Know

If you know which organisation or project the question is about, pass `client`
(and `context` if you know the project). Two things about this:

- **Scope prioritises; it never filters.** Records filed under another client, or
  under none, still come back — ranked lower. This is deliberate. A misclassified
  record must stay findable, so an empty result means "nothing matched", never
  "the filter excluded it".
- **`context` is worth double `client`.** The project is the finer discriminator:
  two people at the same client are told apart by which engagement they are on.

Abbreviations resolve against names already stored — `DB` finds
`Deutsche Bank (DB)` when that is how the client is spelled. A context is
resolved **only against that client's own projects**, so a project name from
another client cannot be paired with the wrong client by accident. Call
`list_clients` when you need the exact stored spellings.

Each result ends with a scope line: `🎯 scope: assigned` (linked by an edge),
`relevant` (linked as also-about), or `inferred` (the name merely appears in the
text). `assigned` from the right client is the strongest evidence you have that
a record belongs to that engagement. A record with no scope line is unscoped —
that is a normal state, not an error, and it is not evidence about the client
either way.

## Confidence Gates, Score Ranks

Facts come back with two different numbers and conflating them is the most
common way to misreport this vault.

- **`confidence`** (0–1) answers *"is this the same record I asked for?"* It is a
  gate. `top_p` filters on it: raise it to `0.75` when you need an exact
  name/alias hit, lower it to `0.35` when a fuzzy name is acceptable.
- **`score`** is a blended ranking value. It is **not** a similarity and must
  never be thresholded. It orders results; nothing else.
- **`evidence`** explains the confidence: `exact`, `alias`, `first+last`,
  `first_name` are the same record. `partial_token`, `fuzzy_name`,
  `surname_strong`, `name_conflict` are near misses.

Results below the gate are not discarded — they come back in an **"Unconfirmed
matches"** section, and that heading is a warning. An unconfirmed result is
**not the same record**. Never link to it, merge it, update it, or report it as
found. If a name search produced nothing confirmed, the person is not in the
vault yet — add them. Editing an unconfirmed near-miss is how two records for one
person get created and never reconciled.

## When a Name Search Comes Back Weak

A near miss on a person is a signal, not a dead end. Search the **first name
only** — surnames are shared and misspelled in transcripts, first names are
not. Then take the confirmed id to `get_fact_neighborhood`, which returns
everything directly connected: the facts they are mentioned in, the diary entries
that reference them, and the client and project they sit under. That is usually
more useful than a second query, because it returns *relationships* rather than
matches.

## What a Hit Actually Is

Records longer than `MEM_CHUNK_CHARS` are stored as several vectors, one per
chunk, all sharing the record's own id. **A hit is always the whole record**, not
the fragment that matched — the text you get back is complete, so do not assume
you are reading an excerpt, and do not join two hits that share an id.

Diary results carry `mentions` (people linked to the entry), `keywords`, and the
resolved `clientName`/`contextName`. Those fields are the entry's own links, not
inferences drawn from the query.

## Reporting What You Found

- Quote the record, not your summary of it, when the wording matters — a
  decision, a commitment, a date.
- Say which entry or fact it came from (date and name for a diary entry), so the
  reader can check you.
- **When the vault disagrees with what the user just said, say so.** A dated
  record beats a recollection, and a contradiction is the most valuable thing a
  memory system produces. Do not smooth it over to agree with the room.
- **When you found nothing, say nothing was found.** Do not answer from general
  knowledge in a way that reads as though it came from the vault. The distinction
  between "the record says" and "I infer" is the whole value of a memory system.
- A null client or project on a diary entry means unclassified *or* classified
  as generic — both are real outcomes. It does not mean the entry is unfinished.

## Narrowing: Dates and People

`client` / `context` rank. `after` / `before` / `person` **remove** — use them
when you already know which records are in question, not to steer the ordering.

- `after` / `before` take an inclusive ISO date or datetime (`2026-05-01`,
  `2026-05-31T14:00`). A date-only bound covers that whole day, so
  `before="2026-05-31"` includes the 31st.
- `person` keeps only records that mention that person by name. The name is
  matched to a stored People record first, so a near-miss spelling lands on the
  record you meant; if nothing resolves, the literal spelling is used and a
  result of nothing means nobody by that name is recorded — say so.
- A record with no date is **outside** every window. That is deliberate: a
  window is a claim about when something happened, and a record that cannot
  answer it does not satisfy it.
- An explicit range **wins** over time phrasing in the query. You do not need
  to also write the date into `query`, and writing it there does not narrow
  anything that the range has not already narrowed.

If a windowed search returns nothing, widen the range before concluding the
vault is empty — the window is the most common reason a real record is absent.

## Checklist

- [ ] Searched the tool that matches the *kind* of thing wanted
- [ ] Passed `client`/`context` whenever they were known
- [ ] Query was terms, not a sentence
- [ ] Read `evidence` before treating a hit as the same record
- [ ] Used `top_p`, never `score`, as the threshold
- [ ] Left unconfirmed matches unlinked and unreported as found
- [ ] Said where each claim came from, and said so when nothing was found
