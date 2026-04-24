---
name: process-transcription
description: This skill provides a structured workflow for processing meeting transcriptions to extract and organize key information into a knowledge graph format.
---

## When to Use This Skill

Use this skill when you have:
- Meeting transcription files (typically .txt files with timestamps)
- Need to extract structured information (people, projects, decisions, action items)
- Want to build or update a knowledge graph with the transcription content
- Need to store extracted information in memory systems for future reference

## Skill Workflow

### 1. File Identification and Preparation
- Locate transcription files in the target directory
- Identify file naming patterns (often date-based: YYYY-MM-DD HH-MM-SS-.txt)
- Create backup or working copies if needed
- Set up output directory structure for organized results

### 2. Metadata Extraction
- Extract date/time from filename
- Identify meeting title/topic from content headers
- Determine meeting context (internal team, client discussion, etc.)
- Note duration if available in content

### 3. Participant and Role Identification
- Scan for speaker labels (e.g., "[00:00:00] SPEAKER:", "[Speaker Name]")
- Extract names and associate with spoken content
- Identify roles/jobs mentioned in discussion (Product Owner, Strategy Lead, etc.)
- Note affiliations/teams when mentioned (SAP, Deutsche Bank, specific departments)

### 4. Content Analysis and Entity Extraction
Process the transcription to identify:

**People:**
- Full names and variations
- Roles and responsibilities
- Contact/context information
- Relationships to projects/topics

**Projects/Initiatives:**
- Project names and descriptions
- Goals and objectives
- Status and timelines
- Stakeholders and owners
- Related initiatives/dependencies

**Technologies/Tools:**
- Specific tools, platforms, systems mentioned
- Versions or specific implementations
- Integration points
- Pros/cons discussed

**Decisions and Action Items:**
- Explicit decisions made
- Action items assigned
- Owners and deadlines
- Follow-up requirements

**Challenges/Risks:**
- Problems identified
- Impact assessments
- Mitigation strategies
- Dependencies/blockers

**Principles/Ways of Working:**
- Methodologies mentioned
- Best practices discussed
- Guidelines or frameworks referenced

### 5. Information Organization
Organize extracted information into categories using .md extension for markdown:

```
Knowledge Graph Structure:
- People/
  - [Person Name].md (with role, context, involvement)
- Projects/
  - [Project Name].md (with goals, status, stakeholders)
- Technologies/
  - [Technology Name].md (with purpose, context, details)
- Decisions/
  - [Decision Topic].md (with context, outcome, owners)
- Challenges/
  - [Challenge Description].md (with impact, mitigation)
- Principles/
  - [Principle Name].md (with definition, application context)
```

Use relative links between files, e.g.:
- Link to person: [[Rafael]]
- Link to project: [[Project Name]]
- Link to principle: [[Principle Name]]

### 6. Entity Resolution and Deduplication
**CRITICAL**: Before creating a new fact (especially for **People** and **Projects**), search the memory to see if the entity already exists.
- Use `search_facts` with the entity name or variations.
- If a match is found:
    - Use `update_fact` to merge new information into the existing record.
    - Do NOT create a duplicate entry.
- If no match is found, use `create_fact`.

### 7. Handling Ambiguity and Mispronunciations (Aliases)
People are often referred to by first names or their names might be mispronounced in the transcription.
- **Aliases**: Create an `aliases` dictionary mapping potential name variations to a confidence score (0.0 to 1.0). For example: `{"Tim Lohman": 1.0, "Tim": 0.7, "Tim Loman": 0.9}`.
- **Flag it**: Call out ambiguous names in your response.
- **Human-in-the-Loop**: If you have low confidence in resolving an entity (e.g., multiple matches for "Tim"), STOP and ask the user for clarification before creating or updating the fact.
- Example: "I see a mention of 'Rafael', but I have two people named Rafael in memory. Should I link this to [[Rafael Papp]] or [[Rafael Silva]]?"

### 8. Memory Storage
Store key facts in the memory system using appropriate categories. **Important: Use rich Markdown formatting in the `text` field for better readability in the Dashboard.**

**Metadata & Tags:**
Always include a `metadata` dictionary with a `tags` list for categorization.
For People, include the `aliases` dictionary in the metadata: `metadata={"tags": ["person", "engineering"], "aliases": {"Tim Lohman": 1.0, "Tim Loman": 0.9, "Tim": 0.7}}`.

**Text Body Enhancements for Search:**
To ensure vector search finds these aliases, append them to the bottom of the Markdown text.
```markdown
### Person: Tim Lohman
**Role:** Lead Engineer
...
**Aliases:** Tim Lohman (100%), Tim Loman (90%), Tim (70%)
```

**Tool Usage:**
- Use `create_fact(text, category, metadata)` for storing NEW facts.
- Use `update_fact(factId, text, category, metadata)` for MERGING into existing facts.
- Use `link_facts(sourceFactId, targetFactId, relationshipType)` for creating relationships.

**Linking Strategy:**
Immediately after creating related facts, use `link_facts` to connect them. This builds the Knowledge Graph.
- Connect **People** to **Projects** (REL: `WORKS_ON`)
- Connect **Decisions** to **Projects** (REL: `DECIDED_FOR`)
- Connect **Action Items** to **People** (REL: `ASSIGNED_TO`)

**Example Markdown Fact:**
```markdown
### Project: Data Migration
**Status:** In Progress
**Priority:** High
- [ ] Complete schema mapping
- [ ] Verify data integrity
- [ ] Sync with [[Rafael]]
```

### 9. Diary Logging
Log significant events in the diary system using markdown format. Use `diary_save_entry(content, date)`.

The diary should contain WHAT THE USER DID, not what was processed. Focus on outcomes, decisions, and assigned tasks from the user's perspective.

### 10. Cross-Reference Creation
Create bidirectional links between related entities using `link_facts`. Ensure every important entity (Person, Project, Technology) is linked to its context.

### 11. Summary Generation
Create summary documents and store them as a comprehensive memory entry in the `work` or `projects` category. Use a header like `# Meeting Summary: [Topic]` and include links to the specific facts created.

## Implementation Guidelines

### Information Extraction Tips
- Look for patterns in speaker introductions
- Note when people state their roles explicitly
- Identify decision points through language like "we decided", "action item", "we will"
- **Formatting:** When creating a fact about a person, include their role and contact info in a structured Markdown block.
- **Graph Thinking:** Always ask "What does this fact relate to?" and create the link.

## Efficiency: Multi-Tool Execution
You are encouraged to call multiple tools in a single response to process the transcription efficiently. You can batch several `create_fact` and `link_facts` calls together, followed by a `diary_save_entry` in one go.

## Customization
This skill can be adapted for different transcription formats by adjusting speaker patterns and category structures.
