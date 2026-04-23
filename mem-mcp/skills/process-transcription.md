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

### 6. Memory Storage
Store key facts in the memory system using appropriate categories. **Important: Use rich Markdown formatting in the `text` field for better readability in the Dashboard.**

- `work`: General work-related information
- `projects`: Project-specific details (use headers, lists, and links)
- `clients`: Client/context-specific information
- `people`: Personnel information and roles
- `decisions`: Specific decisions made (use bold/italics for emphasis)
- `actions`: Action items and follow-ups (use checkbox lists)

**Tool Usage:**
- Use `create_fact(text, category, metadata)` for storing facts.
- Use `link_facts(sourceFactId, targetFactId, relationshipType)` for creating relationships.

**Example Markdown Fact:**
```markdown
### Project: Data Migration
**Status:** In Progress
**Priority:** High
- [ ] Complete schema mapping
- [ ] Verify data integrity
- [ ] Sync with [[Rafael]]
```

### 7. Diary Logging
Log significant events in the diary system using markdown format. Use `diary_save_entry(content, date)`.

The diary should contain WHAT THE USER DID, not what was processed:
- User attended meetings with specific participants
- Decisions made by the user or user's team
- Action items assigned to the user

Example diary entry format:
```markdown
# [Date] - Diary Entry

## Meetings Attended
### Meeting: [Title] ([Time])
- **Participants**: [Names with roles]
- **Summary**: [What was discussed and decided]

## Action Items
- [ ] [Task] - Owner: [Name] - Due: [Date]
```

### 8. Cross-Reference Creation
Create bidirectional links between related entities using `link_facts`.

### 9. Summary Generation
Create summary documents and store them as a comprehensive memory entry in the `work` or `projects` category.

## Implementation Guidelines

### Information Extraction Tips
- Look for patterns in speaker introductions
- Note when people state their roles explicitly
- Identify decision points through language like "we decided", "action item", "we will"
- **Formatting:** When creating a fact about a person, include their role and contact info in a structured Markdown block.

## Example Application

When processing a transcription, the skill would:
1. Extract date, participants, and context.
2. Identify entities (Technologies, Projects, etc.).
3. **Save key facts to memory using `create_fact` (in Markdown format).**
4. **Link related facts using `link_facts`.**
5. **Log timeline information in the diary using `diary_save_entry`.**

## Customization
This skill can be adapted for different transcription formats by adjusting speaker patterns and category structures.
