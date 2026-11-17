# Amiki V2 Engineering Roadmap

**Last verified:** 2026-11-12  
**Current focus:** Phase 1 operational acceptance; both architecture references are established  
**Execution rule:** Inspect → Build → Test → Fix → Verify green  
**Baseline:** Core main aee2ea6; Android main 25824b1; Cloud main c99424c; Cipher main bccee3b.

## Product and engineering principles

- Build small, independently testable slices. Preserve useful V1 behavior; do not rewrite working subsystems for architectural fashion.
- Core owns authoritative conversation metadata, transcripts, prompt context, model orchestration, and specialist execution. Android owns the user-facing conversation list, stable client-generated UUID v4 identity, local cache, and device interaction.
- A conversation ID is an identifier, never an authorization credential. A per-message request ID remains a separate idempotency key.
- Conversation transcripts, bounded prompt context, long-term personal memory, and ALE knowledge are distinct data domains.
- Use existing subsystem ownership: Cloud owns durable goals/tasks/leases and worker supervision; Core owns specialist intelligence; Cipher owns authorization and audit boundaries.
- JARVIS is an architecture reference, not a codebase to clone. Classify any adopted pattern as Inspired, Adapted, or Reused, record its source, and add an Amiki acceptance test.
- Do not claim runtime/device readiness from source inspection or CI alone. Keep GitHub CI, Termux runtime checks, and physical-device acceptance distinct.
- Never ship placeholder agents, pretend tool execution, unbounded autonomy, or inferred capability claims.

## Phase 0 — Audit and architecture references

**Status:** Complete for the initial source audit; refresh against material subsystem changes.

- Backend architecture reference exists at `docs/AMIKI_V2_BACKEND_ARCHITECTURE.md`.
- The Android architecture reference now exists at `docs/AMIKI_V2_ANDROID_ARCHITECTURE.md` in the Android repository. It was merged in PR #97 as 25824b1 and audits source/test structure at 39c0d4c; it does not claim physical-device verification.
- Keep the two documents explicit about subsystem ownership, live entry points, data stores, API contracts, test coverage, dependencies, platform limits, and known risks.
- Preserve existing agent, Cloud, Cipher, and ALE foundations. Do not treat the presence of a module as proof of production readiness.
- Keep the JARVIS source audit as a design reference; adopt patterns only when they solve a demonstrated Amiki problem and have an acceptance test.

**Exit gate:** Both architecture references describe the current default branches; major data flows and trust boundaries are mapped; risks and the next implementation dependency are explicit.

## Backend roadmap

### B1 — Conversational intelligence and output pipeline

**Status:** Conversation identity/storage foundation merged; Phase 1 acceptance remains open.

Completed foundation:
- Typed conversation input and model-result contracts protect the current pipeline boundary.
- Core PR #154 adds validated conversation IDs, SQLite registry/transcripts, lifecycle API, per-conversation context/prompt/persistence, idempotency scoping, and JSON/multipart identity parity.
- Existing legacy global history remains a compatibility path only when a request omits `conversation_id`.

Acceptance still required:
- Prompt-boundary A/B isolation test merged in PR #158 as d5ab946. PR-head API CI run #43177585557 passed; post-merge Core API CI run #43177670716 passed on main.
- Run the merged backend in Termux, restart it, and verify registry and transcript durability.
- Verify JSON and multipart requests end to end against the running backend.
- Verify malformed/unreadable storage fails closed without silent reset.

Next: complete these acceptance gates, then extract or refine pipeline stages only behind typed contracts and regression tests.

### B2 — Persona and conversation awareness

**Status:** Design contract exists; deterministic resolver not implemented.

- Make persona policy explicit, stable, and separate from factual conversation history.
- Implement bounded, deterministic outcomes for continuation, new topic, ambiguous reference, and insufficient context.
- Resolve references only within the selected conversation's bounded history. Explicit topic switches take precedence; multiple plausible antecedents must trigger clarification rather than guessing.
- Keep user-authored history as data, never executable instruction.
- Do not add cross-conversation recall, inferred-topic persistence, or an LLM-only topic classifier in the first resolver slice.
- Add table-driven tests for pronouns, “continue”, option references, topic switches, missing history, ambiguous antecedents, malformed turns, and conversation-boundary isolation.

### B3 — Authoritative Amiki identity specification

**Status:** Existing identity/persona configuration is present; one canonical, reviewed V2 specification is still required.

Define:
- Who Amiki is and what she is for.
- Creator attribution and development-partner attribution without conflating the two.
- Core principles, responsibilities, behavior, and relationship to the owner.
- What Amiki can and cannot do; uncertainty and evidence requirements.
- Authority boundaries: user authorization, privacy, local-device access, tool execution, external services, and prohibited actions.
- How identity, persona, memory, and capability instructions are ordered when they conflict.

The specification must be the source of truth for prompt construction and must be covered by identity/persona regression tests.

### B4 — Core capabilities V2

**Status:** Planned after conversation/persona/identity contracts.

- Audit current capability registry, handlers, and gateway before changing them.
- Define a capability contract with explicit scope, typed input/output, availability, evidence, failure, and side-effect semantics.
- Preserve working capabilities; consolidate overlapping routes and handlers rather than duplicating them.
- Expose capability discovery honestly. A declared capability is not proof that its tools are configured or available.
- Route sensitive or mutating execution through the approved authorization boundary; no silent bypasses.

### B5 — Model-provider orchestration

**Status:** Existing Gemini primary/OpenRouter fallback foundation retained; orchestration hardening remains.

- Keep provider adapters behind a consistent contract.
- Validate response shape and non-empty text at adapter boundaries.
- Add provider health observations and failure classification.
- Use bounded retries, deadlines/timeouts, and rate-limit handling; retry only when safe.
- Make fallback decisions explicit and observable; avoid duplicate side effects or repeated non-idempotent calls.
- Validate final response text and preserve stable error outcomes.
- Manage context size and provider-specific constraints without silently dropping required safety or identity instructions.

### B6 — Individual agent production readiness

**Status:** Specialist modules exist; qualify agents individually before collaboration.

Initial specialists: Orchestrator, Device, Analysis, Research, Tutor, Inspection, Diagnostic, Security, and Engineering.

Every agent must prove:
- Clear role, scope, non-goals, and supported task classes.
- Typed inputs/outputs and evidence/provenance requirements.
- Explicit tool allowlist and real authorized execution.
- Permission checks through the capability/security boundary for sensitive or mutating actions.
- Bounded steps, deadlines, resource use, result size, and cooperative cancellation.
- Classified failures, retry safety, idempotency, and final outcome verification.
- Persistent/resumable task state where promised; otherwise an explicit non-resumable contract.
- Prompt-injection/data-boundary tests, security tests, observability, and repeatable evaluations.

### B7 — Agent collaboration and autonomy

**Status:** Deferred until individual agents pass their own acceptance gates.

- Start at autonomy Level 3 only for qualified agents: bounded multi-step planning and execution using explicitly authorized tools.
- Enable collaboration only among individually qualified agents with typed handoff contracts and a shared evidence model.
- Level 4 long-running work requires durable step state, leases, safe retries, cancellation, and recovery tests.
- Do not rush Level 5. High autonomy is not a release goal by itself.

## Android roadmap

1. **Unified V2 UI/UX:** Establish one design system and centralized theme tokens; make light and dark themes consistent across every screen.
2. **Duplicate-action audit:** Identify duplicate navigation, controls, actions, and overlapping screens; consolidate without removing useful workflows.
3. **Session and lifecycle recovery:** Harden active conversation restore, backend reconnect, interrupted requests, pending drafts, remote transcript hydration, and process-death recovery.
4. **Android Sentinel:** Build on the existing Sentinel foundation with truthful device/runtime status, durable event handling, and bounded resource use.
5. **Direct agent interaction:** Provide agent-specific panels with text and voice input plus TTS, clear execution state, and cancellation/error feedback.
6. **Biometric access:** Use Android-supported biometric APIs and enforce authorization at the protected operation boundary; do not rely on a decorative lock screen.
7. **Permission-aware device integration:** Discover actual device capabilities, request only required permissions, and report platform/API restrictions honestly.

**Device constraint:** Benchmark on the Infinix HOT 9 class device (Android 10/API 29, 3 GB RAM) before enabling expensive background or continuous-interaction behavior. Keep text-first operation usable.

## Ecosystem boundaries

- **Cipher:** Retain essential identity/core protection and authorization/audit duties. Defer Cipher X expansion; do not add a second execution path or weaken deny-by-default policy.
- **Amiki Cloud:** Maintain existing health, task, lease, worker, and recovery functionality. Do not broaden Cloud scope unless a V2 acceptance gate requires it; do not create a competing task database in Core.
- **ALE:** Preserve owner review, evidence provenance, and separation between conversation transcripts and approved knowledge. Improve learning integration only when a demonstrated quality issue and regression test justify it.
- **Workspace/JARVIS-inspired patterns:** Adapt architecture ideas selectively; never copy code or add features merely because the reference project has them.

## Release and verification gates

1. **Architecture gate:** Backend and Android architecture references are current and their data/security boundaries are understood.
2. **Conversation gate:** A/B prompt isolation, backend restart persistence, JSON/multipart parity, and Android A → B → A/process-death acceptance pass.
3. **Identity/persona gate:** Canonical identity and persona behavior are deterministic, bounded, and regression-tested.
4. **Capability/provider gate:** Capability contracts and provider failure/fallback behavior pass unit and integration tests.
5. **Agent gate:** Each specialist passes its own scope, permission, evidence, cancellation, security, failure, and evaluation suite.
6. **Autonomy gate:** Collaboration and higher autonomy are enabled only after qualified agents and durable recovery are demonstrated.
7. **Android gate:** Theme consistency, lifecycle recovery, Sentinel, biometrics, and device integrations are tested on supported Android versions and the target device.

No phase is complete by code presence or CI alone.

## Current integration checkpoint — 2026-11-12

- Core main: aee2ea64eca28863366cf26f7c7bc15db8f8ab66. Core PR #154 was merged as ecc0cd22418fe4fb20a0c9c0fe1fb849804c07d9; PR #158 as d5ab946b9fa10e2ec946d698ae1b0dbb40fa5757; docs checkpoint PR #159 as aee2ea64eca28863366cf26f7c7bc15db8f8ab66.
- Android main: 25824b173e2470f5808335d55478f3dc65df6c0a. Android PR #96 is merged as 39c0d4ce8330c7321d839aed7b6a43043e2f8a63; architecture audit PR #97 is merged as 25824b173e2470f5808335d55478f3dc65df6c0a.
- Cloud main: c99424c63cf4046562906fbc18ebb14d76e4a6c9. Cipher main: bccee3b858a99080e1bae6b0d6191c81643c8d51. No Cloud or Cipher changes are part of this slice.
- Post-merge Core API CI passed on current main (run #43177954691). Android architecture PR CI passed with unit tests and debug APK build (run #43178272651); post-merge Android main run #43178491452 was still in progress when this checkpoint was written. These results do not establish Termux runtime or physical-device acceptance.
- Prompt-level A/B isolation test and roadmap/architecture alignment are merged. PR-head API CI run #43177585557 and post-merge main CI run #43177670716 both passed.
- Android source review found two remaining conversation acceptance gaps: remote transcript hydration failure has no explicit retry state, and device-local actions are not mirrored into Core transcripts.
- The Android architecture reference is now present. Phase 0's initial documentation gate is satisfied; the next work is operational acceptance and the documented recovery gaps.

**Immediate next step:** sync both local main branches, complete Termux backend restart/JSON-multipart acceptance, then implement Android hydration retry and process-death recovery before starting the deterministic conversation-awareness resolver.
