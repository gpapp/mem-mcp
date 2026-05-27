"""
memory.py – Shared library for the Memory Vault (Facade).

Imports and exposes configuration, database clients, and helpers
from common.py, fact_manager.py, and diary_manager.py.
"""

from common import (
    QDRANT_URL, NEO4J_URL, NEO4J_USER, NEO4J_PASS, OLLAMA_URL, EMBED_MODEL,
    HTTP_TIMEOUT, BASE_URL, COLLECTION_NAME, DIARY_COLLECTION,
    SESSION_SECRET, SESSION_MAX_AGE, db_subscribers,
    publish_db_event, get_qdrant, get_neo4j, wait_for_service,
    get_embedding, extract_user_from_headers, logger
)

from fact_manager import (
    extract_people_metadata, db_add_memory, db_update_memory, db_delete_memory,
    db_link_facts, db_unlink_facts, db_get_neighborhood, db_get_fact_by_id,
    db_get_connections_by_type, db_search_memories, db_find_patterns,
    db_list_memories, db_list_categories, db_find_duplicates, db_merge_memories,
    db_get_graph, run_consistency_checks, sync_orphans
)

from diary_manager import (
    _diary_id, db_save_diary, db_update_diary, db_search_diary, db_delete_diary,
    db_link_diary_mention, db_unlink_diary_mention,
    db_list_diary_entries, db_list_diary, run_diary_consistency_checks, fix_diary_entries
)
