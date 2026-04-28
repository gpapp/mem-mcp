---
name: cleanup-transcription
description: Refines raw transcription into a speaker-labeled, cleaned version with a summary, leveraging memory for entity identification.
---

## When to Use This Skill

Use this skill when you have:
- A raw, unrefined transcription (often with generic speaker labels like "SPEAKER 1").
- A need for a readable, clean version of the transcript.
- A requirement to accurately identify who said what.
- A need for a concise summary of the discussion.

## Skill Workflow

### 1. Context and Entity Discovery
- **Identify Metadata**: Extract date, project context, and potential clients from the filename or header.
- **Search Memory**: Use `search_facts` to find:
    - **Projects/Clients**: Facts related to the meeting topic.
    - **Potential Participants**: Search for people linked to the identified projects or clients.
    - **Corrections**: Look for facts in the 'Corrections' category to fix spelling or technical terms.

### 2. Participant Verification
- **Ask for Participants**: If the list of participants is not provided by the user or clear from memory, **STOP and ask the user** for a list of who was in the meeting.
- **Note**: Not all participants necessarily spoke during the meeting.

### 3. Speaker Identification and Turn Correction
- **Identify Speakers**: Map generic labels (e.g., "SPEAKER_01") to actual participant names.
- **Direct Addressing**: Look for instances where speakers address each other by name (e.g., "John, what do you think?").
- **Deductive Guessing**: If addressing is not present, use the participants list and the content of the speech (roles, topics mentioned) to guess the speaker.
- **Accuracy**: Maintain consistent speaker labeling throughout the transcript.

### 4. Transcription Cleanup
- **Tool**: Use `transcription_cleanup` with the transcription text and the identified participants list.
- This tool handles the removal of filler words and speaker identification on the server.
- **Final Review**: Review the cleaned output and ensure any "Stored Corrections" from memory were applied correctly.

### 5. Final Output Structure
Produce the final output in two parts:

#### A. Cleaned Transcript
```markdown
[00:00:10] **John Doe**: Welcome everyone. Today we are discussing...
[00:00:45] **Jane Smith**: I have an update on the frontend progress.
...
```

#### B. Main Points Summary
- **Key Decisions**: Summarize explicit decisions made.
- **Action Items**: List tasks with their owners and deadlines.
- **Project/Client Links**: Explicitly link the summary to the relevant [[Project]] and [[Client]].

## Implementation Guidelines

- **Entity Linking**: Ensure all mentioned people and projects are linked using [[Name]] syntax.
- **Ambiguity**: If you are unsure about a speaker identification, flag it (e.g., "[00:12:00] **Unidentified (possibly John?)**").

## Efficiency: Multi-Tool Execution
You are encouraged to call multiple tools in a single response. For example, you can call `transcription_cleanup` and then process the results with multiple `create_fact`, `link_facts`, and `diary_save_entry` calls in one go.
