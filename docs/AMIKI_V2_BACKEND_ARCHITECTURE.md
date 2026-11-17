# Amiki V2 Backend Architecture Audit

**Audit date:** 2026-11-10  
**Audited ref:** `main` at `f73d71028240b61801646ea65d4a0e2f8161df34` (re-based for integration on 2026-11-10)  
**Status:** Initial repository-level architecture audit; not a penetration test or a live-device acceptance test.

## Executive assessment

Amiki V1 is not a blank prototype. It has a working, modular backend with a substantial conversation pipeline, context and persistent-memory components, capability handlers, specialist agents, provider fallback, observability, an owner-reviewed learning system, and a FastAPI surface. Backend CI was green for the audited `main` commit.

V2 should preserve these verified foundations and improve their contracts and boundaries incrementally. The most immediate maintainability problem is repository hygiene: `core/` contains 215 entries, including 212 top-level files; 152 of those files use historical snapshot suffixes such as `.pre_*`, `.post_*`, `.working`, `.backup`, or `.verified`. These are tracked source-tree clutter and make the live source of truth harder to identify. Do not delete them in bulk: first prove they are not imported, referenced by tooling, or intentionally retained as rollback evidence, then archive/remove them in a separate reviewed change.

## Runtime architecture

### Entry points and lifecycle

- `interfaces/api/server/app.py` creates the FastAPI application, includes the central API router, and uses an application lifespan hook.
- The optional Cloud autonomy worker starts only when `AMIKI_CLOUD_AUTONOMY_WORKER_ENABLED` is truthy. The worker is given a stop event and joined with a bounded shutdown wait.
- `main.py` is a small interactive CLI entry point that delegates to `core.router.chat`.
- `interfaces/api/routes/` separates API areas including chat, agents, capabilities, learning, memory, device, diagnostics, Cloud, Cipher, voice, workspace, and observability.

### Conversation path

The live conversation orchestrator is `core/router.py`:

1. Validate and ingest typed attachments; extract text/image context and emit bounded observability events.
2. Extract and classify memory candidates; apply explicit remember/forget operations. Capture explicit correction feedback for review rather than silently promoting it.
3. Resolve a capability: explicit agent directives, detected learning requests, configured capability keywords, concrete workspace paths/actions, then automatic specialist routing.
4. Dispatch through a registered capability handler, the learning request handler, or the `AgentGateway`.
5. Capture source-backed research candidates for review.
6. Build the model prompt from identity, persona, conversation/context, memory, relevant approved learning knowledge, specialist dispatch results, reasoning awareness, attachments, and location context.
7. Call the configured model adapter.
8. Store the user message; store the assistant message only for a non-error response. Publish request trace success/failure and observability events.

This is a useful existing spine. V2 should make each stage's input/output contract explicit and separately testable before changing the orchestration order.

### Identity, persona, context, and memory

- `config.json` currently holds assistant metadata, provider/model selection, capability flags, academic defaults, identity, and persona policy.
- `core/prompt.py` builds identity and Persona V2 instructions separately and combines them with context.
- `core/context.py`, `core/context_manager.py`, `core/memory_*.py`, and the learning engine provide session context, persistent memory, retrieval, classification, and owner-reviewed knowledge.
- Existing tests cover identity/persona composition and behavior.
- `docs/ALE_V2_FOUNDATION.md` describes a domain-independent curriculum registry and explicitly states that provisional lesson completion is not proof of mastery, autonomous research, or model-weight training. Preserve those honesty boundaries.

### Capability and agent layer

- `core/capabilities.py` is the configuration/availability registry.
- `core/handlers/` contains focused handlers for analysis, university, workspace, web, device, and Noctis Requiem.
- `core/agents/` contains contracts, gateway, orchestrator, manager, collaboration coordinator, tools, and specialist implementations (research, tutor, engineering/code, inspection/diagnostics, device, and security).
- `core/agents/contracts.py` normalizes and bounds specialist results. The gateway is the policy boundary for specialist invocation.
- Agent and API test suites cover gateway, contracts, dispatch, collaboration, and several specialist behaviors.

The agent architecture is present, but presence of a module does not prove that every specialist is production-ready. Each agent still needs an explicit permission matrix, reliable result/evidence contract, per-agent evaluations, and operational failure tests.

### Model/provider layer

- `core/model.py` reads provider/model settings from `config.json` and API keys from environment variables.
- Gemini is the configured primary provider. It uses bounded retries for selected transient statuses and can fall back to OpenRouter.
- OpenRouter currently uses the generic `openrouter/free` model route.
- Both provider adapters use 60-second request timeouts. Gemini retry delays are fixed and bounded.
- `core/request_trace.py` records stages, provider attempts, fallback decisions, final provider/model, and request outcome.

**First V2 gap identified:** the OpenRouter adapter assumes the decoded JSON is a mapping and that `choices[0].message.content` is a non-empty string. The Gemini extraction path similarly trusts parts of the provider response shape. Valid JSON with an unexpected shape or non-string content can produce an unhandled attribute/type error or leak a non-string result into the router, whose pipeline calls string methods on the response. This is the first bounded code improvement in this branch: normalize and reject invalid provider text at the adapter boundary, with regression tests.

## Tests and CI

- Root-level tests cover routing, capability handlers, memory, context, attachments, model behavior, persona, workspace, web, and university flows.
- `interfaces/api/tests/` contains focused API and agent tests, including model response, provider fallback, runtime-state isolation, observability, learning review, diagnostics, security, workspace, and Cloud-worker contracts.
- `.github/workflows/api.yml` runs on Python 3.12, installs `interfaces/api/requirements.txt`, and runs the API contract plus selected regression modules.
- The audited `main` commit had a successful API CI run.
- CI is not the same as a Termux runtime check or a full test of all tracked root tests. The workflow's explicit command is a selected test list; V2 should eventually make the complete intended test matrix explicit and reproducible.

## Key risks and technical debt

| Priority | Finding | Evidence / consequence | V2 direction |
|---|---|---|---|
| High | Historical source snapshots tracked inside `core/` | 152 of 212 top-level files match historical-copy naming patterns | Inventory references, document retention purpose, archive/remove in a separate PR |
| High | Provider payload shape is not fully validated | Adapters assume mapping/list/string shapes from external JSON | Normalize provider output at adapter boundary; fail closed with stable errors |
| High | Conversation pipeline is tightly orchestrated in one module | Attachment, memory, routing, prompt, model, persistence, and trace all live in `core/router.py` | Extract stage contracts incrementally; preserve public `chat` behavior |
| Medium | Runtime configuration is loaded at import time | `core/model.py` and `core/prompt.py` read config during import | Introduce validated immutable settings object in a later isolated stage |
| Medium | Provider selection is only partly abstracted | Primary settings are config-driven; OpenRouter fallback route and behavior are hard-coded | Introduce explicit provider adapters/registry after contract tests |
| Medium | Capability routing uses keyword heuristics | Several broad words can trigger routes; fallback is order-sensitive | Add intent classification only behind deterministic routing tests and confidence/fallback rules |
| Medium | Historical root and package test coverage differs from CI | Workflow executes a selected list rather than every root test | Define named unit, contract, integration, and evaluation suites |
| Medium | Runtime persistence is local-file-oriented | Memory and learning use local JSON/store files | Keep data durability and migration semantics explicit; do not silently reset malformed stores |

## Constraints and non-goals

- This audit did not inspect private runtime secrets or live Termux state.
- This audit did not execute Python tests in the repository environment; CI evidence is used only for the audited commit.
- No V1 subsystem is being declared fully complete based on source presence alone.
- Do not rewrite the whole router, replace ALE, or expand Cipher X/Cloud as part of the first V2 foundation work.

## Build order

1. **Provider text contract** — validate external response shapes and enforce one non-empty string result per adapter. Add regression tests.
2. **Conversation-stage contracts** — specify typed request, context, capability dispatch, model result, and persisted-turn boundaries without changing behavior.
3. **Runtime settings** — validate and inject config rather than reading global settings during imports.
4. **Provider orchestration** — add explicit provider capabilities, health observations, failure classification, bounded retries, and duplicate-execution safeguards.
5. **Pipeline extraction** — move stages into focused units only when contract tests protect behavior.
6. **Agent hardening** — individual responsibilities, permissions, evidence, cancellation, limits, evaluations, and failure behavior before collaboration/autonomy expansion.
7. **ALE integration** — improve source/evidence/correction quality only where tests demonstrate user value.

## Audit conclusion

The backend has enough useful foundations to evolve; a wholesale rewrite would throw away working behavior and increase risk. V2 starts by tightening the response boundary, then makes the current pipeline's contracts explicit and separates it in small, test-backed steps.


## V2 implementation progress

### Slice 1 — provider response contract (2026-11-10)

- Added shape and text validation for OpenRouter and Gemini responses.
- Added malformed-payload regression coverage.
- GitHub Actions API CI passed: 743 tests, 8 warnings.

### Slice 2 — conversation input contract (2026-11-10)

- Added `core/conversation_contracts.py` with a frozen `ConversationInput` boundary.
- The pipeline now validates message type, optional capability type, attachment sequence/items, and location-context mapping before starting side effects.
- Attachment order is preserved and location context is copied to prevent caller mutation during the run.
- Added unit and pipeline-entry regression tests.
- GitHub Actions API CI passed: 764 tests, 8 warnings in 13.07 seconds.
- No local Termux run is claimed.

**Next slice:** define and validate the model-stage result contract (success text versus stable provider error) at the pipeline boundary, then use those contracts to separate pipeline stages without changing user-visible behavior.


### Slice 3 — model-stage result contract (2026-11-10)

- Added `core/model_result_contracts.py` with an immutable `ModelResult` outcome.
- The contract distinguishes successful text, provider failures, invalid provider responses, empty responses, and invalid/non-string adapter results.
- `run_pipeline()` now normalizes the legacy `ask_model()` string at the model boundary and uses the typed outcome for trace status and assistant-message persistence.
- Existing public chat/API behavior remains string-based; valid response text and existing `ERROR:` messages are preserved.
- Added parameterized contract tests for success, provider error, malformed response, empty output, and invalid result types.
- GitHub Actions API CI run [#42770315549](https://github.com/leoperish54-hiro/Amiki-/actions/runs/42770315549) completed successfully. No local Termux run is claimed.

**Next slice:** add targeted pipeline integration coverage for model success/failure persistence and trace outcomes, then begin extracting the prompt/model/persistence stages behind their tested contracts.


### Slice 4 — pipeline persistence and trace integration tests (2026-11-10)

- Added `interfaces/api/tests/test_model_result_pipeline.py` using the real `run_pipeline()` and `RequestTrace`, with external storage, provider, and observability effects isolated.
- Verified that successful model output persists the user and assistant turns, completes the model/context stages, and publishes a successful request event.
- Verified that provider errors persist only the user turn, fail the model stage, omit the assistant stage, and publish a failed request event.
- Verified that non-string and empty model results fail closed with stable error strings and never create an assistant turn.
- GitHub Actions API CI run [#42780294715](https://github.com/leoperish54-hiro/Amiki-/actions/runs/42780294715) completed successfully. The workflow's selected API contract/regression test command passed. No local Termux run is claimed.

### Conversation-context audit notes for the next V2 slice

The existing system already has basic context retrieval; it is not a blank slate and should be preserved:

- `core.context.add_message()` stores role/text turns in one bounded current-session JSON list, with atomic file replacement and an in-process re-entrant lock.
- `core.context_manager.build_context()` combines lexical conversation retrieval with long-term memory retrieval.
- `select_relevant_context()` ranks messages by token overlap, uses recency as a tie-breaker, and can fill unused slots with recent messages. It preserves chronological order.
- `core.prompt.build_prompt()` supplies the selected history to the model and includes existing identity/persona, approved learning knowledge, active learning-project, and system-awareness sections.
- This is useful basic continuity, but there is no explicit active-topic/turn-reference model. Short references such as “that”, “it”, “the second one”, and topic switches are left to the model to infer from selected text; lexical overlap alone does not resolve them.
- `load_context()` accepts any JSON list without validating each turn's role/text shape. The current record format also has no explicit turn ID, timestamp, or topic metadata.

**Next slice:** specify a small, backwards-compatible conversation-context contract and deterministic tests for continuation, explicit topic change, ambiguous references, and malformed stored turns. Do not add heuristic entity resolution or rewrite the prompt until those expected behaviors are agreed and protected by tests.

### Repository freshness and integration note

- The backend architecture audit was initially performed against `main` at `da24be04cbd87871e26e7c0ed65ed0d96b105d60`.
- Current `main` is `ce3d8d18a1616971f637a4188ac54723c32e6239`. Since the audit baseline, it has received two separate Core Cloud worker-supervision changes: bounded worker recovery (#144) and sanitized supervisor recovery-event reporting (#145).
- The V2 feature branch has not been rebased onto those two newer main commits. GitHub currently reports the PR as mergeable, but it remains unmerged and two commits behind `main`. No force update or merge was performed. Re-check the base/head and required CI before merging; green feature-branch CI does not replace Termux or physical-device acceptance where those are required.


### Slice 5 — enforce model-result construction invariants (2026-11-10)

- Added runtime invariants to the frozen `ModelResult` type: success requires non-empty text and no error; failures cannot carry answer text and must carry a stable `ERROR:` response; unsupported statuses are rejected.
- Added regression tests for inconsistent direct construction so callers cannot create contradictory success/error states outside the normalizer.
- GitHub Actions API CI run [#42781175938](https://github.com/leoperish54-hiro/Amiki-/actions/runs/42781175938) completed successfully.



### Slice 6 — legacy conversation-turn validation (2026-11-10)

- Added a shared validator for the existing `{role, text}` session-turn shape. Only non-empty text with established `user` or `assistant` roles is admitted to model context.
- `load_context()`, relevance scoring, and recent-message fallback all apply the same validator. Malformed entries cannot reach prompt context through either loading or direct selection.
- Prompt-facing filtering is read-only. Raw session records are preserved during normal appends and memory-retention trimming; unreadable or structurally invalid stores fail closed instead of being overwritten.
- The memory-settings endpoint uses `trim_context()` so it does not silently discard legacy records filtered from model context.
- Regression coverage includes malformed role/text fields, direct context selection, fallback ordering, append/trim preservation, boolean limits, and unreadable-store protection.
- Latest-head GitHub Actions run [#42858087384](https://github.com/leoperish54-hiro/Amiki-/actions/runs/42858087384) on `a03dead3d51cdbc32bbfd73991ce0e62edaf1df8`: **757 passed, 8 warnings**.
- PR #146 was squash-merged into `main` as commit `f73d71028240b61801646ea65d4a0e2f8161df34`. The post-merge main-branch API workflow [#42859006172](https://github.com/leoperish54-hiro/Amiki-/actions/runs/42859006172) subsequently completed successfully.
- No local Termux or physical-device run is claimed.

**Next slice:** finish integration of the backend architecture foundation PR #143, then implement the deterministic conversation-awareness contract. Main-chat conversation identity must be designed before multi-session persistence or inferred active-topic state is introduced.


### Integration checkpoint — clean foundation rebase (2026-11-10)

- The original foundation PR #143 was closed without merging after its branch diverged from current main.
- Its nine reviewed changed files were re-applied to a fresh branch from main commit `f73d71028240b61801646ea65d4a0e2f8161df34`; no force-push or history rewrite was used.
- The refreshed integration is PR #148. Its current-head API workflow is required before merge; earlier successful runs on the old branch do not verify the rebased head.

## Roadmap alignment update (2026-11-11)

The canonical cross-repository build order is now maintained in docs/AMIKI_V2_ROADMAP.md. This supersedes older forward-looking “Next slice” notes in this historical audit where they conflict with the accepted multi-conversation identity decision.

Phase 1 now spans Core and Android pull requests: Core implements the durable registry/transcript store, lifecycle API, conversation-scoped chat/context, idempotency separation, multipart parity, and backend tests; Android implements stable per-conversation IDs, local cache, New Chat/History controls, and both request payload forms. The two PRs remain unmerged and end-to-end/device acceptance is still outstanding. Multiple independent conversations are not considered operational until the full Phase 1 acceptance gate passes.



## Integration checkpoint — 2026-11-12

- The durable multi-conversation backend/API work from PR #154 was squash-merged to Core main as ecc0cd22418fe4fb20a0c9c0fe1fb849804c07d9.
- Current Core main is d5ab946b9fa10e2ec946d698ae1b0dbb40fa5757. The conversation merge ecc0cd2 and prompt-isolation PR #158 are included. Subsequent Core Cloud outbox/supervisor work (#156 and #157) is present on main but was not changed by this V2 slice.
- Android PR #96 was also merged to its main as 39c0d4ce8330c7321d839aed7b6a43043e2f8a63. Phase 1 implementation is integrated in both repositories; operational acceptance is still open.
- Added a prompt-level A/B isolation regression to exercise the real core.prompt.build_prompt() path with the SQLite conversation store. It asserts that a prompt built for A contains neither user nor assistant sentinel turns from B, and vice versa. This closes a gap in the earlier store-only isolation test. PR-head API CI run #43177585557 passed, and post-merge Core API CI run #43177670716 passed on d5ab946.
- Android source inspection found bounded local cache limits (20 conversations, 80 messages per conversation, 8,000 characters per cached message), remote transcript hydration, and two remaining limitations: hydration failures have no explicit retry state, and device-local actions are not mirrored into Core transcripts.
- No Termux runtime, backend process-restart, or physical-device process-death test is claimed here. Keep these as explicit Phase 1 acceptance gates.

The older pre-merge status notes above are historical. This checkpoint supersedes any statement that PR #154 or PR #96 remains unmerged.


### Slice 7 — structured provider-neutral model requests (2026-11-13)

- Added `core/model_request.py` with immutable `ModelRequest` and `ModelMessage` contracts.
- The request separates system instructions, runtime reference context, and ordered user/assistant conversation turns.
- `core.prompt.build_model_request()` constructs the structured request while `build_prompt()` retains its legacy flattened-string contract for existing specialist callers and compatibility tests.
- Prompt section boundaries are captured by the prompt builder itself rather than inferred by searching generated text. Runtime context containing prompt-like headings cannot move untrusted data into the system instruction.
- The main `run_pipeline()` path now sends the structured request to the provider layer.
- Gemini receives the system instruction through `systemInstruction` and conversation turns through role-labelled `contents` (`assistant` maps to Gemini's `model` role).
- OpenRouter receives a system message followed by runtime context and ordered user/assistant messages.
- Image parts remain attached to the final user turn for both provider adapters. Existing string-based model calls retain their legacy payload shape.
- Provider adapters coalesce adjacent same-role turns and normalize assistant-only leading transcript evidence so request histories remain provider-compatible.
- Conversation-awareness outcomes, capability policies, location safeguards, self-knowledge rules, and grounding policies remain in the system instruction. Retrieved memories, transcript content, attachments, and capability results are explicitly treated as reference data rather than instruction sources.
- Added provider/request regression coverage for role mapping, multimodal payloads, legacy compatibility, location policy, and prompt-boundary injection. Updated pipeline contract tests to assert the structured request boundary.
- GitHub Actions API CI run [#43429310022](https://github.com/leoperish54-hiro/Amiki-/actions/runs/43429310022) completed successfully: **927 passed, 8 warnings**.
- No Termux regression, on-device runtime, or provider-live test is claimed. Those checks remain part of local acceptance after the branch is merged.
