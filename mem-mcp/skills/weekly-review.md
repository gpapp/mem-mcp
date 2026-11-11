---
name: weekly-review
description: How to run a weekly review across every client — what happened, what is still open, who has gone quiet and what needs a decision — in the order that produces one readable page rather than five searches.
---

# Skill: Weekly Review

## When to Use This Skill

Friday afternoon, Monday morning, or any time somebody asks "where are we
across everything?" — the question that is easy to answer for one engagement and
surprisingly hard to answer for twelve.

This is a **read** workflow. Nothing here writes.

## The Order

The order is the product. Gathering by client first produces twelve small
pages that nobody reads; gathering by question produces four answers that
compose into one.

1. **The week's meetings.** `list_diary_entries(fromTs="<monday>", toTs="<next
   monday>")`. This gives you what actually happened, in the vault's own words,
   with the client and project on each entry. Note the ones whose name suggests
   a decision was taken.

2. **What closed, and what is still open.** `list_action_items` twice: once with
   no filter for everything open, and once per client when you need the detail.
   `status="done"` for what closed this week, and `dueBefore="<today>"` on the
   open set for what is now late. **Overdue is computed at read time**, never
   stored — so "overdue" is a statement about today, not about the record.

3. **What was decided.** `list_decisions` per client, or `client_timeline` for
   one engagement. Read the superseded rows too: a decision that was replaced
   is the most useful thing on the page, because it is what somebody will
   otherwise re-litigate in the next meeting. A decision that quietly stopped
   being followed is worse than one that was never logged.

4. **Who has gone quiet.** `stakeholder_roster("<client>")` per client. The
   roster reports last-mentioned dates and flags people past the staleness
   threshold. A quiet stakeholder is the thing a weekly review exists to
   surface: nothing is wrong, which is exactly why no meeting notes mention it.

## The Page You Are Producing

Four sections, in this order, and none of them is a raw dump:

1. **What moved** — meetings that produced a decision, a closed commitment, or
   a change of plan, one line each, with the client named.
2. **What is late** — overdue commitments with their owner and how late. Group
   by client, most overdue first. If nothing is late, say so explicitly: "on
   track" and "nobody wrote anything down" must not read the same.
3. **What needs a decision** — anything where the last decision is superseded,
   reversed, or contradicted by a newer meeting.
4. **Who I have not spoken to** — the quiet stakeholders, with how long it has
   been, and which client they belong to.

Then one line of judgement: what you would do on Monday. That is the part a
human cannot get from the four sections, and it is the reason they asked.

## Rules That Keep the Page Honest

- **Never invent a date.** A commitment with no due date is undated, not "soon".
  `list_action_items` returns `dueAt` as null when the source text named no
  date, and the parser deliberately refuses to guess — a weekday phrase resolves
  against the source record's own date, never against today.

- **A person with no date is unmeasured, not stale.** A stakeholder nobody has
  written about has no last-mentioned date at all. Report them as "nothing
  recorded", never as "quiet for N days" — the second is an invented
  measurement, and it is the one an agent is most likely to produce because it
  fits the sentence better.

- **A closed commitment stays closed.** If the register says `done`, the work
  was finished; do not reopen it because the minutes still contain the bullet.
  Re-extraction reconciles and never reopens what a human marked.

- **An empty week is a finding, not a failure.** If `list_diary_entries`
  returns nothing for the window, say that and stop. Do not widen the window
  silently to have something to report — a review that quietly covers three
  weeks while claiming one is how a stale engagement stays invisible.

- **Scope filters here, unlike search.** `list_action_items` and `list_decisions`
  take `client` / `context` as a **filter** — they answer a closed question, so
  an item from another engagement is a wrong answer rather than a lower-ranked
  one. This is the opposite of `search_facts`, and confusing the two is why
  agents report "nothing open for this client" when the real answer is "nothing
  *but* other clients' items came back".

- **Do not merge a duplicate to tidy up.** If the review surfaces two records
  that look like one, `find_duplicates` and the review-first merge flow exist —
  but a merge is irreversible and wants a human's confirmation, not a weekly
  review's convenience.

## A Worked Shape

```text
Week of 9 November — 4 clients, 6 meetings, 11 open commitments (2 late)

What moved
  Deutsche Bank / DB AI Adoption — steering agreed to defer the data
    migration to Q1; two commitments closed.
  SAP SE — nothing recorded this week.

What is late
  EPAM / PPC — "send the architecture doc" (Tim), 6 days late.
  Deutsche Bank — "confirm the licence count" (unassigned), 2 days late.

Needs a decision
  SAP SE — the licence decision from 12 October is superseded and the
    replacement was never written down. Ask before the next call.

Gone quiet
  Joao Bonin (SAP SE) — 71 days, threshold 60.
  Priya Raman (EPAM) — nothing recorded at all.

Monday: chase the two late items, and put the SAP licence question on the
agenda — it is the only thing here with no owner.
```

The shape matters more than the prose. Every line above is a thing somebody can
act on, and nothing above is a fact the vault did not already hold.
