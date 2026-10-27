"""
memory.py – Shared library for the Memory Vault (Facade).

Imports and exposes configuration, database clients, and helpers
from common.py, fact_manager.py, diary_manager.py, and client_manager.py.
"""

from common import (
    QDRANT_URL, NEO4J_URL, NEO4J_USER, NEO4J_PASS, OLLAMA_URL, EMBED_MODEL,
    LLM_QUERY_MODEL, EXTRACT_MODEL, SCOPE_MODEL, MERGE_MODEL,
    LIVE_MODEL, LIVE_UNLOAD_ON_RECORD, ASR_WS_URL,
    HTTP_TIMEOUT, BASE_URL, COLLECTION_NAME, DIARY_COLLECTION,
    SESSION_SECRET, SESSION_MAX_AGE, db_subscribers,
    publish_db_event, get_qdrant, get_neo4j, wait_for_service, ensure_ollama_models,
    get_embedding, get_llm_response, extract_user_from_headers, logger,
    STATUS_POLL_SECONDS, configured_model_roles, fetch_ollama_status, unload_ollama_model,
    is_admin_user, ADMIN_USER, claim_maintenance, release_maintenance, active_maintenance
)

import status_monitor

from fact_manager import (
    extract_people_metadata, db_add_memory, db_update_memory, db_delete_memory,
    db_add_fact_relevant, db_remove_fact_relevant,
    db_link_facts, db_unlink_facts, db_get_neighborhood, db_get_fact_by_id,
    db_get_connections_by_type, db_search_memories, db_find_patterns,
    db_list_memories, db_list_categories, db_find_duplicates, db_merge_memories,
    db_get_graph, run_consistency_checks, sync_orphans, rechunk_unindexed_records
)

from diary_manager import (
    _diary_id, db_save_diary, sync_diary_mentions, db_update_diary, db_update_diary_metadata, db_search_diary, db_delete_diary,
    db_link_diary_mention, db_unlink_diary_mention,
    db_add_diary_relevant, db_remove_diary_relevant,
    db_list_diary_entries, db_list_diary, run_diary_consistency_checks, fix_diary_entries,
    _auto_link_people, find_people_candidates,
)

from client_manager import (
    db_create_client, db_create_context, db_list_clients,
    db_resolve_client, db_resolve_context,
    db_owner_of_target,
    link_fact_to_client, link_fact_to_context,
    link_diary_to_client, link_diary_to_context,
    db_set_client_active, db_set_client_cross, db_rename_client, db_rename_context,
    db_set_fact_scope, db_set_diary_scope, db_client_items, db_context_items,
    db_delete_client, db_delete_context, db_get_client_status_map,
    db_plan_search_scope,
    resolve_write_scope,
    infer_scope_from_text
)

from live_manager import (
    NOTE_CATEGORY, TRANSCRIPTION_STATUSES, LIVE_GATE,
    db_create_transcription, db_list_transcriptions, db_get_transcription,
    db_update_transcription, db_set_transcription_scope,
    db_delete_transcription,
    db_append_segment, db_list_segments, db_transcription_state,
    db_update_segment, db_delete_segment, db_mark_segments_consumed,
    db_get_note_for_transcription, db_set_note_transcription,
    db_get_note_references, db_set_note_references,
    db_note_fact_candidates, release_note_segments,
    start_note_job, note_job_status, NoteJobDeferred, NoteJobBusy,
)

# The WebSocket origin check is a pure helper; keeping it in `live_notes.py`
# means the rule can be tested without a running server.
from live_notes import (
    LiveNoteError, LiveNoteTooLong, RecordingActiveError,
    origin_allowed as live_notes_origin_allowed,
    asr_languages_url,
)
