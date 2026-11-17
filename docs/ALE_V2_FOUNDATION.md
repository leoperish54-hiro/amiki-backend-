# ALE V2 Foundation: Curriculum and Progress

## Scope

ALE V2 adds a validated, versioned Software Engineering foundations curriculum and
persistent lesson-attempt tracking. The bundled pathway is preparatory general
material. It is **not** an official University of Zambia course outline, a
university assessment, or a claim about the current UNZA syllabus.

The initial catalog covers:

1. Computing foundations: binary representation, computer architecture, shell use,
   and development environments.
2. Python programming: values, control flow, functions, modules, collections,
   exceptions, debugging, and tests.
3. Mathematical reasoning and algorithms: logic, sets, functions, pseudocode,
   complexity, and core data structures.
4. Software engineering practice: Git, decomposition, HTTP/APIs, relational
   databases, and secure development foundations.

The catalog lives in
`curricula/software_engineering_foundations.json`. Its loader validates schema
version, required text, unique identifiers, prerequisite references, duplicate
prerequisites, and cyclic dependencies before returning curriculum data.

## Domain-independent learning architecture

The Software Engineering pathway is the first curriculum pack, not the boundary
of ALE. The registry discovers validated JSON packs in `curricula/*.json`.
Each pack has its own stable `curriculum_id`, title, courses, modules, lessons,
objectives, prerequisite graph, assessment descriptors, and provisional
completion policy. New packs can describe any subject; the engine does not
contain Software Engineering-specific lesson logic.

The registry exposes `get_curricula()`, `get_curriculum(curriculum_id)`,
and curriculum-scoped lesson lookup. Progress records are namespaced by
`curriculum_id`, so identical lesson IDs in different subjects cannot unlock
or overwrite one another. Older unscoped attempts remain associated with the
original Software Engineering pack. Retention protects passing results across
all currently registered packs.

Owner-authenticated API routes:

- `GET /api/v1/learning/curricula`: list available subject packs.
- `GET /api/v1/learning/curricula/{curriculum_id}`: read one pack.
- `GET /api/v1/learning/curricula/{curriculum_id}/progress`: read subject progress.
- `POST /api/v1/learning/curricula/{curriculum_id}/progress/{lesson_id}/attempt`:
  record progress in that subject.

The original singular Software Engineering routes remain as compatibility
aliases. A new subject is added as a validated curriculum pack; no new
subject-specific engine implementation is required.

This registry is the reusable curriculum and progress foundation. It does not
yet autonomously research every user request, generate and teach a complete
course, execute arbitrary assessments, or establish mastery. Those capabilities
must be built as separate, testable stages. The goal is to let an owner request
a subject while keeping source provenance, assessment evidence, review controls,
and progress isolated by subject.

## Progress semantics

The API exposes:

- `GET /api/v1/learning/curriculum`: read the validated catalog.
- `GET /api/v1/learning/progress`: inspect the owner's lesson progress.
- `POST /api/v1/learning/progress/{lesson_id}/attempt`: record an assessment result.

Progress reads and writes require the configured owner bearer token. The catalog
contains no private learner progress and is read-only.

A lesson attempt records a bounded assessment identifier, type, score, optional
evidence reference, and timestamp. Duplicate assessment identifiers for the same
lesson are idempotent. Prerequisites must have a recorded passing result before a
dependent lesson attempt can be stored.

A score at or above the catalog's provisional pass threshold marks a lesson as
**provisionally completed**. It does not independently prove understanding,
mastery, or skill verification. It never calls `record_skill`, never auto-approves
knowledge, and never updates provider model weights. A separate assessment engine
must eventually execute or independently grade exercises before ALE can claim
verified competence.

## Store integrity

The learning store schema is now version 2 and adds `lesson_attempts`. Schema-v1
stores are migrated in memory without dropping existing known collections; the
next successful write persists the new schema. Unknown top-level fields are
preserved.

Malformed JSON, invalid collection shapes, and unsupported future schema versions
raise `LearningStoreError`. The implementation does not replace unreadable data
with an empty store. Writes remain atomic through a temporary file, fsync, and
replace. Protected pending and approved knowledge is not silently evicted when
the bounded collection is full; the operation fails with
`LearningCapacityError` instead. Lesson-attempt retention removes unsuccessful
attempts first and only removes a repeated passing attempt when another passing
result for that same lesson remains. It refuses to evict the only passing result
for any lesson. Progress summaries ignore attempts for lesson IDs no longer
present in the active catalog.

Approved-knowledge retrieval now uses a deterministic, bounded BM25-style lexical
ranking. It still retrieves approved records only; candidates pending owner review
remain inaccessible to prompt retrieval.

## Next engineering stages

1. Independent assessment execution for quizzes and coding exercises.
2. Evidence-aware skill verification that cannot be asserted by a caller's boolean.
3. Research corroboration, source quality grading, and contradiction tracking.
4. Adaptive lesson sequencing and spaced revision based on assessment history.
5. Held-out evaluations for learning transfer and regression detection.

Do not describe these future stages as implemented until their code and tests exist.
