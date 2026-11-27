# Persistent memory contract

This document describes the current file-backed personal-memory implementation. It is separate from the Adaptive Learning Engine's reviewed knowledge records.

## Storage format

The configured `memory.long_term_file` contains one JSON object:

```json
{
  "facts": [
    {
      "category": "preference",
      "key": "favorite_vehicle",
      "value": "example coupe"
    }
  ]
}
```

The document must contain a `facts` array. Every fact must be an object with non-empty string fields `category`, `key`, and `value`. The manager currently accepts the categories `preference`, `personal_fact`, and `project_fact`; the storage layer does not reject additional category names so older or future records remain readable.

Additional document and fact fields are preserved during updates. A missing file represents a new empty store. A present file with invalid JSON, invalid UTF-8, an invalid root, a missing or malformed `facts` array, or malformed fact fields is not an empty store.

## Failure and recovery behaviour

- Reads of damaged or inaccessible files raise `MemoryStoreError`. They never return an empty store as a fallback.
- Ordinary remember, update, and forget operations fail closed and do not replace a damaged file.
- Writes use a unique temporary file in the target directory, restrictive file permissions, file flush and fsync, and atomic replacement. A failed replacement leaves the previous file in place.
- A process-local re-entrant lock serializes reads and writes across threads in one backend process. The current file store is intended for a single backend writer process; multi-process or multi-device writers require a transactional shared store such as SQLite with an explicit migration.
- `clear_memory()` and the owner-authorized persistent-memory DELETE endpoint are intentionally destructive. They can replace an unreadable store with an empty store only when explicitly invoked; they are not automatic corruption recovery.

The memory read API returns HTTP 503 when persistent memory is unavailable. Chat can continue, but its prompt is marked to say memory could not be accessed rather than claiming that no memories exist. Diagnostics reports the storage failure without exposing file contents or internal exception details.

## Extraction and retrieval

Extraction is deliberately conservative and pattern-based. It recognises supported explicit facts and preferences, including corrections, and does not store arbitrary conversation text or hypothetical future plans. Unsupported or ambiguous phrasing may not be captured.

Retrieval is local and deterministic: exact key matching, curated natural-language aliases, and bounded lexical evidence are ranked together. Compound questions can return more than one strong concept. General questions such as “What do you remember about me?” return the most recent bounded set of facts. The default retrieval limit is five.

This is not embedding-based semantic search. It does not yet provide automatic memory consolidation, temporal history, fact supersession history, per-fact confidence/provenance, or cross-process locking. Those capabilities require a separately designed schema and migration rather than silently changing the existing store format.

## Privacy and trust boundaries

Persistent memory values are contextual data, not system instructions. They must not override the system or owner authorization policies. Do not log raw memory contents, publish runtime memory files, or add real owner details to public test fixtures.