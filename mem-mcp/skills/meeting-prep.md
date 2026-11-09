---
name: meeting-prep
description: How to brief yourself before a meeting — in what order to gather an engagement's open commitments, recent meetings and scope facts, what to lead with, and what to say out loud when the vault does not know.
---

# Skill: Meeting Prep

## When to Use This Skill

Before a meeting, a steering committee, a status call, or any conversation
where you are expected to arrive already knowing where an engagement stands.
Also when someone asks "what is going on with <client>?" and you want one
answer rather than five searches.

This is a **read** workflow. Nothing here writes.

## The Order Matters

Gather in this order. Each step narrows what the next one needs to look at, and
the order is also the order the reader will want to hear it in.

1. **Where this stands.** `list_clients` — the exact client name, whether it is
   active, and which projects it has. Project names matter later: they are the
   finer discriminator, and a brief that names a project nobody recognises is
   worse than one that names none.
2. **What is owed.** `list_action_items(client="<client>")`, then again with
   `context="<project>"` when the meeting is about one project. Overdue first.
   This is the section the meeting will actually be about.
3. **What happened.** `diary_search_entries` with the client name, and
   `diary_search_entries` with the project name. Or `list_diary_entries` for a
   date window when you know roughly when you last spoke. Read the entry names
   and dates — you do not need every body.
4. **What we know.** `search_facts` for the client, then for the project, then
   for the specific people who will be in the room.
5. **Who.** `search_facts(category="People", client="<client>")` for the roster
   you are walking into.

If step 1 returns nothing, stop and say so. Every later step will return
other people's data or nothing at all, and reporting a stranger's engagement
as if it were this client's is the failure mode worth avoiding.

## What to Lead With

- **Overdue commitments before anything else.** A date that has passed is the
  one thing in the brief that has changed on its own.
- **Then what is due next**, with owner and date read exactly as stored.
- **Then what changed** — the most recent meetings, newest first.
- **Then the standing context** — the facts that are true regardless of when
  the meeting is.

Do not open with a summary of what the client does. The reader knows. They are
asking what is *different* since the last time they looked.

## Use the Tool, or Follow It By Hand

`client_brief(client, context=None)` does all five steps and writes the page.
Prefer it: it renders the same material whether or not the model is available,
so a model outage costs polish and not information.

Do the steps by hand when you need only part of the answer, when the brief is
stale and you want it rebuilt, or when the scope is ambiguous and you would
rather resolve the client name yourself.

## Rules That Are Not Style

- **Everything you say must point at a record.** A name, a date or a number that
  is not in the vault is an invented detail, and an invented detail in a
  briefing is worse than a missing one because the reader will repeat it to the
  client.
- **Read owners and dates exactly as stored.** Do not normalise "2026-05-31"
  to "end of May", and do not rename a stakeholder to a name you prefer.
- **Say what is missing, plainly.** "No commitments are recorded for this
  client" is a fact about the vault and is worth knowing before the meeting —
  it may mean nobody writes actions down, which is itself the finding.
- **A commitment marked done is done.** Do not reopen it because the bullet is
  still in the minutes.
- **Distinguish "not recorded" from "does not exist".** A null client or project
  on a record means unclassified or generic, not that the engagement has no
  history.

## Checklist

- [ ] Client resolved to its stored spelling, and confirmed active or inactive.
- [ ] Open commitments gathered, overdue ones identified.
- [ ] Recent meetings read, newest first, and only the relevant ones.
- [ ] People in the room looked up, with the roles on file.
- [ ] Something said explicitly about anything the vault does not know.
- [ ] No invented names, dates, numbers or decisions.
