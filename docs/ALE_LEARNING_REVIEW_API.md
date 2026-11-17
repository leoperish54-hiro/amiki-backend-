# ALE Learning Review API

The Learning Review API exposes pending learning candidates to the owner and
provides explicit approval/rejection controls. Research results remain pending
until the owner reviews their content, sources, and evidence.

## Authorization

All owner-protected ALE routes use **UOSA**, not the retired
`AMIKI_OWNER_REVIEW_TOKEN` bearer.

- L1 sessions are short-lived (15 minutes), bound to one enrolled device
  generation, audience-limited to `amiki-core`, and limited to explicitly
  requested scopes.
- Review-queue reads require `ale.review.read` (L1).
- Approve and reject operations require `ale.review.approve` or
  `ale.review.reject` (L2).
- L2 authorization is issued only after a fresh device-key signature and is
  valid for one request, one scope, and the exact HTTP method, path, query, and
  canonical JSON body. Its lifetime is two minutes.
- Replaying the grant, changing the record ID, or changing the request body
  invalidates the authorization.
- The Android device performs biometric verification locally and signs the
  server challenge with its enrolled P-256 key. Fingerprint templates, face
  data, and biometric sensor data are never sent to the backend.

The old owner-review bearer no longer authorizes ALE routes. It must not be
restored as a fallback or recovery mechanism.

## Review endpoints

All paths below are under `/api/v1`.

| Endpoint | Required UOSA scope | Level |
|---|---|---:|
| `GET /learning/review-queue?kind=all&limit=100` | `ale.review.read` | L1 |
| `POST /learning/review-queue/{record_id}/approve` | `ale.review.approve` | L2 |
| `POST /learning/review-queue/{record_id}/reject` | `ale.review.reject` | L2 |
| `GET /learning/knowledge-map` | `ale.knowledge.read` | L1 |
| `POST /learning/knowledge/{record_id}/extract-claims` | `ale.claims.extract` | L2 |
| `GET /learning/claims/{claim_id}/evidence-assessment` | `ale.claims.review` | L1 |
| `POST /learning/claims/{claim_id}/evidence-evaluation` | `ale.claims.evaluate` | L2 |
| `POST /learning/knowledge-map/semantic-analysis` | `ale.claims.analyze` | L1 |

Progress, curriculum-progress, and project routes are separately scoped:
`ale.progress.read/write` and `ale.projects.read/write` (L1).

### List pending candidates

`GET /learning/review-queue?kind=all&limit=100`

- `kind`: `all`, `knowledge`, or `experience`.
- `limit`: 1–200.
- Returns pending records with content, source, confidence, timestamps, and
  retained evidence/provenance.
- Does not return approved or rejected records.

### Approve knowledge

`POST /learning/review-queue/{record_id}/approve`

Only a pending knowledge candidate can be approved. Correction experiences cannot
be promoted as knowledge. Approval makes the knowledge eligible for ALE retrieval.

### Reject a candidate

`POST /learning/review-queue/{record_id}/reject`

Rejects a pending knowledge candidate or correction experience. Decisions cannot
be repeated against already-reviewed records.

## Enrollment and recovery

First-owner setup is a separate, one-time ceremony. Run
`python scripts/create_uosa_setup_code.py` from the backend repository on the
trusted host, then enter the displayed high-entropy code into the initial device
enrollment flow. It expires after 15 minutes and is consumed by the first
successful P-256 device enrollment. The backend removes the local setup-code
handoff file after successful enrollment.

The first enrollment returns ten one-time recovery codes. Store them offline;
the server stores only their SHA-256 hashes. A successful recovery invalidates
all previously enrolled devices, sessions, and outstanding challenges, consumes
the submitted recovery code, and rotates the full recovery-code set. A current
owner can also rotate recovery codes using the L2
`uosa.recovery.manage` scope.

See `docs/UOSA_SECURITY_RUNBOOK.md` for operational setup, secondary-device
enrollment, key rotation, recovery, and deployment constraints.

## Trust boundaries

The API does not prove the truth of a research candidate. The owner must inspect
its source and evidence before approval. External snippets remain untrusted data.
ALE does not update provider model weights or perform fine-tuning.

UOSA L3 is deliberately not enabled by this release. Cipher X, emergency
isolation, and other critical actions require a separately designed and tested
authorization authority; an Amiki Core session must never confer those rights.
