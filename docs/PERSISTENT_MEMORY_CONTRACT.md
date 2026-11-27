# Persistent memory contract

Personal memory is a separate subsystem from the Adaptive Learning Engine. It stores owner-specific facts and conversational preferences; it is not a model-weight training pipeline and stored content is never treated as executable instruction.

## Storage and migration

SQLite is the source of truth at `memory/persistent_memory.sqlite3` (configurable through `memory.database_file`). The previous `memory/persistent_memory.json` file is imported once on first access and deliberately retained untouched as a recovery copy. A corrupt or invalid legacy file blocks automatic migration; Amiki does not silently replace it with an empty database. An explicit owner-authorized clear operation is the recovery path.

The database uses:
- SQLite WAL mode and FULL synchronous durability.
- Foreign-key enforcement and a configurable busy timeout.
- `BEGIN IMMEDIATE` transactions for mutations and a partial unique index enforcing one active value per category/key.
- SQLite's cross-process locking rather than a process-local file lock.
- Restrictive database file permissions where supported.

Each operation opens its own connection. SQLite serializes writers across processes. The first-run JSON migration rechecks its completion marker after acquiring the write transaction so concurrent processes cannot import the same legacy facts twice.

## Temporal facts

A fact is never overwritten in place when its value changes. The previous row becomes `superseded`, receives a `valid_to` timestamp and points to the new fact through `superseded_by`. The new row becomes active with its own identifier, creation time and `valid_from` timestamp.

Explicit forgetting changes the active row's status to `forgotten` and records an event; it does not physically erase history. Explicit owner-authorized clear similarly records forgotten states. The history API returns both fact versions and audit events. This history is useful for continuity and corrections, but it is not a legal-grade immutable audit log: an owner-authorized clear, database deletion or filesystem-level modification can remove it.

## Provenance and confidence

Each fact stores:
- A numeric confidence score in the inclusive range 0.0–1.0.
- Source type and extraction method.
- Capture timestamp, optional correlation identifier and original source statement.
- A provenance object that can hold later evidence and migration metadata.

The conversation extractor currently marks captured user statements as pattern-extracted conversation evidence with a default confidence of 0.90. This is a heuristic, not a calibrated probability. Repeated confirmation adds a reinforcement event and evidence entry, and can raise the stored confidence. Legacy imports are marked as such and receive a conservative default confidence.

The original source text is retained to support review. Do not place credentials, secrets or unrelated sensitive content in provenance. Runtime memory databases and model caches must not be committed to the repository.

## Semantic retrieval

Exact keys, curated aliases and lexical relevance remain available as deterministic fallbacks. Optional semantic ranking uses normalized local embeddings and cosine similarity, persisted alongside each fact in SQLite. Existing facts are lazily backfilled when semantic embeddings are enabled.

Semantic embeddings are opt-in because the model has a non-trivial download, storage and RAM cost. To enable:
1. Install `requirements-memory-embeddings.txt`.
2. Set `memory.embeddings.enabled` to `true` in the local `config.json`.
3. Keep the configured model/cache local to the device or server.

The default provider is FastEmbed with `BAAI/bge-small-en-v1.5`. No embedding model is downloaded when the setting is disabled. If the optional provider is unavailable, lexical and curated retrieval continue to work. The model is not fine-tuned on personal data.

## Consolidation and conflict review

The consolidation analyzer groups:
- Likely duplicates (normalized identical values or very high embedding similarity).
- Potential contradictions (different values attached to the same fact key).
- Semantically related facts (embedding similarity above the configured review threshold).

The analyzer is read-only. It does not automatically merge, delete or rewrite facts. A contradiction is a review candidate, not proof that one statement is false. Temporal supersession is handled separately and should not be misclassified as a live contradiction.

The `GET /memory/consolidation` endpoint previews current active facts. The `GET /memory/history` endpoint exposes historical versions and events. Both require the existing `memory.read` UOSA scope.

## API and recovery behaviour

- `GET /memory` returns active facts with provenance and confidence.
- `GET /memory/history` returns version history and event records.
- `GET /memory/consolidation` returns a read-only review report.
- The existing persistent-memory DELETE endpoint is explicitly destructive and remains protected by its UOSA scope.
- Corrupt or unavailable storage returns a safe service-unavailable response; chat may continue without pretending memory is empty.

## Extraction and trust boundaries

Extraction remains conservative and pattern-based. It does not store arbitrary conversation text or hypothetical plans. Unsupported phrasing may not be captured. Memories are contextual evidence, not system instructions; they cannot override system policy, owner authorization or tool permissions.

## Operational limits

- Embeddings are opt-in and currently require a supported FastEmbed runtime and a one-time model download.
- Similarity scores and confidence values are ranking/review signals, not truth guarantees.
- Consolidation identifies candidates; it does not autonomously resolve contradictions.
- SQLite supports concurrent local processes on a shared filesystem. It is not a multi-device replication protocol; multiple devices need a shared transactional service or explicit synchronization design.
- The legacy JSON file remains as a migration/recovery copy and should be backed up securely with the SQLite database.
