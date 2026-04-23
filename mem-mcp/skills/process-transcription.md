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
Store key facts in the memory system using appropriate categories:
- `work`: General work-related information
- `projects`: Project-specific details
- `clients`: Client/context-specific information
- `people`: Personnel information and roles
- `decisions`: Specific decisions made
- `actions`: Action items and follow-ups

Use mem0_create_fact for storing facts and mem0_link_facts for creating relationships between facts.

### 7. Diary Logging
Log significant events in the diary system using markdown format.

The diary should contain WHAT THE USER DID, not what was processed:
- User attended meetings with specific participants
- Decisions made by the user or user's team
- Action items assigned to the user
- Key discussions and outcomes from user's perspective
- Personal learnings and reflections

Example diary entry format:
```markdown
# [Date] - Diary Entry

## Meetings Attended

### Meeting: [Title] ([Time])
- **Participants**: [Names with roles]
- **Summary**: [What was discussed and decided]

## Key Outcomes
- [Decision 1]
- [Decision 2]

## Action Items
- [ ] [Task] - Owner: [Name] - Due: [Date]
```

Use mem0_diary_save_entry to save diary entries.

### 8. Cross-Reference Creation
Create bidirectional links between related entities:
- Person → Projects they're involved in
- Project → Technologies used
- Decision → People who made it
- Challenge → Related projects/technologies
- Principle → Applications/contexts

### 9. Summary Generation
Create summary documents that include:
- Executive summary of meeting
- Key decisions and action items
- Notable participants and their contributions
- Follow-up requirements and owners
- Links to detailed category files

## Implementation Guidelines

### File Processing Order
1. Process memo/summary files first (they often contain pre-extracted key points)
2. Process full transcription files for detailed validation and additional context
3. Handle different file naming conventions appropriately

### Information Extraction Tips
- Look for patterns in speaker introductions
- Note when people state their roles explicitly
- Watch for project names mentioned repeatedly
- Identify decision points through language like "we decided", "action item", "we will"
- Extract challenges through phrases like "problem", "challenge", "risk", "concern"
- Note principles through statements like "we always", "best practice is", "our approach is"

### Quality Assurance
- Cross-check information between memo files and full transcriptions
- Verify speaker identification consistency
- Confirm action item ownership and deadlines
- Validate dates and timelines
- Ensure proper categorization of extracted information

## Example Application

When processing a transcription like:
`C:\Users\Gergely_Papp\Videos\DB\2025-06-20 10-57-12-memo.txt`

The skill would:
1. Extract date: 2025-06-20
2. Identify participants: Rafael, other names
3. Recognize context: Data platform discussion
4. Extract entities:
   - Technologies: MDM, AWP, BigQuery, analytical mart
   - Processes: Data preparation, training/validation, deployment pipeline
   - Challenges: Data registration, code storage, AWP requirements
   - Projects: "Model Deployment Process" project file
5. Store in appropriate categories:
   - People: Individual files for each participant (.md)
   - Projects: "Model Deployment Process" (.md)
   - Technologies: Files for MDM, AWP, BigQuery (.md)
   - Decisions: Key architectural decisions made
   - Challenges: Identified implementation challenges
6. Save key facts to memory using mem0_create_fact
7. Link related facts using mem0_link_facts
8. Log significant timeline information in diary using mem0_diary_save_entry (in markdown format)
9. Create cross-references between related entities
10. Generate summary document

## Customization

This skill can be adapted for different transcription formats by adjusting:
- Speaker label patterns
- Date extraction methods
- Category structures based on domain
- Memory categories used
- Output detail level
