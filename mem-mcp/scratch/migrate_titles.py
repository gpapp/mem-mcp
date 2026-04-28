import asyncio
import re
import os
import sys

# Add current dir to path
sys.path.append(os.getcwd())

# Override URLs for local execution (assuming ports are forwarded)
os.environ["MEM_NEO4J_URL"] = "bolt://localhost:7687"
os.environ["MEM_QDRANT_URL"] = "http://localhost:6333"

import memory as mem

async def migrate_facts():
    neo4j_driver = mem.get_neo4j()
    if not neo4j_driver:
        print("Error: Could not connect to Neo4j.")
        return

    print("Fetching all facts from Neo4j...")
    with neo4j_driver.session() as s:
        # We fetch all facts for all users to be thorough, 
        # but db_update_memory needs a specific user_id.
        result = s.run("MATCH (f:Fact) RETURN f.id as id, f.text as text, f.userId as userId")
        facts = [dict(r) for r in result]

    print(f"Found {len(facts)} facts. Starting migration...")

    migrated_count = 0
    failed_facts = []

    # Regex to match **Title** - Body or **Title**\nBody or just **Title**Body
    # It looks for text between double asterisks at the start.
    pattern = re.compile(r"^\*\*(.*?)\*\*\s*(?:-\s*|\n\s*)?(.*)", re.DOTALL)

    for fact in facts:
        fact_id = fact["id"]
        original_text = fact["text"] or ""
        user_id = fact["userId"]

        match = pattern.match(original_text)
        if match:
            title = match.group(1).strip()
            body = match.group(2).strip()
            
            if not body:
                # If there's only a title, maybe it's not a full fact yet or the regex missed the body
                failed_facts.append({"id": fact_id, "text": original_text, "reason": "No body found after title"})
                continue

            print(f"Migrating [{fact_id}]: '{title}'")
            try:
                # Use the new db_update_memory which now supports 'title'
                success = await mem.db_update_memory(
                    memory_id=fact_id,
                    text=body,
                    title=title,
                    category=None, # Keep existing
                    user_id=user_id
                )
                if success:
                    migrated_count += 1
                else:
                    failed_facts.append({"id": fact_id, "text": original_text, "reason": "Update failed (not found?)"})
            except Exception as e:
                print(f"Error updating {fact_id}: {e}")
                failed_facts.append({"id": fact_id, "text": original_text, "reason": str(e)})
        else:
            failed_facts.append({"id": fact_id, "text": original_text, "reason": "Pattern not matched (**Title** ...)"})

    print("\n" + "="*50)
    print(f"MIGRATION COMPLETE")
    print(f"Successfully migrated: {migrated_count}")
    print(f"Failed/Skipped:        {len(failed_facts)}")
    print("="*50)

    if failed_facts:
        print("\nFACTS THAT COULD NOT BE MIGRATED:")
        for f in failed_facts:
            print(f"- ID: {f['id']}")
            print(f"  Reason: {f['reason']}")
            snippet = (f['text'][:100] + '...') if len(f['text']) > 100 else f['text']
            print(f"  Text: {snippet}")
            print("-" * 20)

if __name__ == "__main__":
    asyncio.run(migrate_facts())
