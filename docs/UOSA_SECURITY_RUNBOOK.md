# UOSA Security Runbook

## Security model and intended boundary

UOSA (Unified Owner Security & Authorization) is the backend authority for
owner identity, enrolled devices, session scopes, elevation, revocation, and
recovery. Authentication and authorization remain separate:

- A device proves possession of its private P-256 key by signing a fresh
  server challenge.
- On Android, the key should be held by Android Keystore and the signing
  operation gated by a fresh local biometric prompt. Biometric material never
  leaves the device.
- The server verifies the signature and enforces the audience, scope, device
  generation, expiry, revocation, and transaction binding.
- A Core owner session does not grant authority to Cipher, Cipher X, or other
  services. Service-to-service tokens remain distinct credentials.

The current implementation enables L0 (enrollment/recovery result only), L1,
and L2. L3 is intentionally unavailable pending separate federation and
critical-action policy work for Cipher X / emergency isolation. Do not treat an
L1 or L2 Core session as an L3 capability.

## First-owner setup (one time)

1. Run the backend on the trusted local host and verify that the Core API is
   reachable only on the intended interface.
2. From the backend repository root, run:

   ```sh
   python scripts/create_uosa_setup_code.py
   ```

3. Copy the displayed one-time code directly into the first-device enrollment
   flow. Do not paste it into chat, store it in the repository, or include it
   in logs.
4. The setup code expires after 15 minutes and is single-use. The backend
   stores only its SHA-256 hash in SQLite and writes a protected handoff copy
   with owner-only permissions. Successful initial enrollment consumes the
   code and deletes the handoff file.
5. Initial enrollment returns ten recovery codes once. Save them offline in
   a trusted password manager or equivalent secure offline record. The server
   stores only hashes. Do not put the plaintext recovery codes in logs or
   repository files.

The setup-code command refuses to run after any device/recovery state has ever
been initialized. It cannot be used to silently recreate a first owner after
all devices have been revoked; use the recovery ceremony instead.

## Additional device enrollment

A device is not enrolled by presenting the old owner bearer. An existing
enrolled device must:
1. Obtain an L1 Core session containing the `uosa.device.enroll` scope.
2. Authorize the new device's enrollment challenge.
3. Complete the enrollment call with an L2 grant bound to the exact POST path
   and canonical request body.
4. Verify the new device proves possession of its own P-256 key.
5. Establish a separate session on the new device.

The backend enforces the maximum of five active devices, unique key
fingerprints, device-generation binding, pending-challenge limits, and
revocation of stale sessions/challenges. Newly enrolled devices receive no
inherited session token.

## Session and elevation lifetimes

- Challenges: 120 seconds, one-time, bound to the device, audience, scope,
  fingerprint, enrollment generation, and (for elevation) transaction digest.
- L1 sessions: 15 minutes, audience `amiki-core`, explicitly requested scopes.
- L2 grants: 120 seconds, one use, one scope, one exact method/path/query/body.
- A successful request consumes the L2 grant. A mismatch or replay is denied.
- Device revocation and key rotation invalidate sessions and outstanding
  challenges for that device. Sessions are also rejected if their stored
  device generation no longer matches the enrolled device.

Do not add wildcard scopes, long-lived owner sessions, global unlock, or
client-side-only authorization checks.

## Device key rotation

The current device obtains an L1 session with `uosa.device.rotate`, then
requests a rotation challenge for a newly generated replacement key. Both the
old and replacement private keys must sign the same challenge. The final
rotation request requires a transaction-bound L2 grant. Success increments
the device generation, changes the stored public key/fingerprint, and revokes
all of that device's prior sessions and pending challenges. The device must
then establish a fresh session with the new key.

A replacement key fingerprint already assigned to any device is rejected.
Do not delete the old key until the server confirms successful rotation and a
new session has been established.

## Lost-device recovery

Recovery requires one unused recovery code and proof of possession of a newly
generated P-256 private key. Recovery is rate-limited and the challenge is
single-use. Successful recovery:
- consumes the submitted recovery code;
- revokes all previously enrolled devices and active sessions;
- consumes outstanding challenges;
- enrolls the replacement device;
- rotates the entire recovery-code set and returns the new codes once.

A recovered device must then request a fresh L1 session. There is no fallback
to the retired owner bearer, no automatic reuse of old device keys, and no
partial account-wide unlock.

Recovery attempt tracking is persisted by a hash of the client address. When
hosting behind a reverse proxy, configure a trusted proxy boundary and ensure
the ASGI client address reflects the real client correctly; do not blindly
trust arbitrary forwarded-for headers. This default local deployment uses
loopback and should not be exposed directly to the public internet.

## Recovery-code rotation

A currently enrolled owner can rotate the recovery-code set with an L2
`uosa.recovery.manage` authorization. Existing codes are revoked immediately.
The replacement codes are returned once; save them offline before closing the
response. A rotation must be bound to the exact empty-body POST request.

## Current authorization scope registry

| Scope | Level | Purpose |
|---|---:|---|
| `ale.review.read` | L1 | Read pending ALE review queue |
| `ale.progress.read` / `ale.progress.write` | L1 | Read/write lesson progress |
| `ale.projects.read` / `ale.projects.write` | L1 | Read/write learning projects |
| `ale.knowledge.read` | L1 | Read approved knowledge map |
| `ale.claims.analyze` / `ale.claims.review` | L1 | Analyze claims and inspect evidence |
| `ale.review.approve` / `ale.review.reject` | L2 | Approve/reject candidates |
| `ale.claims.evaluate` / `ale.claims.extract` | L2 | Evaluate evidence and extract claims |
| `academic.read` / `academic.write` | L1 | Read/update academic records |
| `agents.run` | L2 | Run agent orchestration |
| `chat.use` | L1 | Use chat and voice |
| `conversations.read` / `conversations.write` | L1 | Read/create conversation records |
| `cipher.inspect` | L1 | Read Cipher status and run bounded inspection |
| `device.read` / `device.plan` | L1 | Read device state and plan an action |
| `device.execute` | L2 | Execute a device action |
| `memory.read` / `memory.session.clear` | L1 | Read memory and clear current-session context |
| `memory.persistent.clear` | L2 | Delete persistent memory |
| `settings.read` | L1 | Read owner settings |
| `settings.write` | L2 | Change model or memory settings |
| `observability.read` | L1 | Read private events and diagnostics |
| `workspace.read` | L1 | Read workspace files, projects, proposals, and Git state |
| `workspace.proposals.write` | L1 | Create or reject proposed changes |
| `workspace.project.create` | L2 | Create a workspace project |
| `workspace.change.apply` | L2 | Approve and apply a proposed change |
| `workspace.file.write` | L2 | Write or create a workspace file |
| `workspace.file.execute` / `workspace.command.execute` | L2 | Execute workspace files or commands |
| `cloud.autonomy.read` | L1 | Read Cloud autonomy state |
| `cloud.autonomy.stop` | L1 | Stop Cloud autonomy (safe stop operation) |
| `cloud.autonomy.resume` | L2 | Resume Cloud autonomy |
| `cloud.jobs.read` | L1 | Read Cloud jobs |
| `cloud.jobs.cancel` | L1 | Cancel a Cloud job |
| `cloud.jobs.create` / `cloud.jobs.run` | L2 | Create or execute a Cloud job |
| `cloud.goals.read` | L1 | Read goals, schedules, and runs |
| `cloud.goals.pause` | L1 | Pause or cancel a goal |
| `cloud.goals.create` / `cloud.goals.resume` / `cloud.goals.update` | L2 | Create, resume, or update a goal |
| `cloud.schedules.create` / `cloud.schedules.manage` | L2 | Create or control a scheduled goal |
| `cloud.monitor.read` | L1 | Read monitor status and events |
| `cloud.monitor.acknowledge` | L1 | Acknowledge a monitor event |
| `cloud.outbox.inspect` | L1 | Inspect local Cloud dead-letter queue |
| `cloud.outbox.retry` / `cloud.outbox.restart` | L2 | Retry/restart local Cloud outbox |
| `uosa.device.read` | L1 | Read enrolled-device metadata |
| `uosa.security.audit.read` | L1 | Read metadata-only UOSA audit events |
| `uosa.device.enroll` | L2 | Enroll an additional device (L1 preflight, L2 completion) |
| `uosa.device.rotate` | L2 | Rotate an enrolled device key (L1 preflight, L2 completion) |
| `uosa.device.revoke` | L2 | Revoke an enrolled device |
| `uosa.recovery.manage` | L2 | Rotate one-time recovery codes |

L2 scopes are included in the requested session scope list but are not active in
an L1 session. To use one, the owner obtains a fresh challenge and signs it
locally; the resulting L2 grant is one-use and bound to that exact operation.
L2 grants are burned on the first attempted protected request, including an
incorrect path, scope, or body.

The session-challenge endpoint is rate-limited to 30 issuance attempts per
client address in five minutes, with a two-minute lockout after the limit.
Recovery uses a stricter persisted per-client attempt limit. Pending challenges
are capped per device, expire after two minutes, and expired ephemeral token
records are pruned during subsequent challenge issuance. The current address
source is the ASGI client address; forwarded headers are not trusted.

`GET /health`, static capability/catalog routes, and the first-owner setup
ceremony remain public by design. Sentinel event ingestion remains a local
machine-ingress endpoint in this release and is not suitable for untrusted
network exposure; it needs a separate device-attested telemetry identity before
remote hosting. Cloud and Cipher service credentials remain separate from
UOSA owner sessions.

Scope selection is explicit at session issuance; never request more scope than
the operation needs. Keep Cloud's API token and Cipher's read-only token as
separate service credentials.

## Operational file and database protections

- UOSA SQLite defaults to `$HOME/.local/state/amiki/uosa.sqlite3`; override
  only with an absolute `AMIKI_UOSA_DB_PATH`.
- The database directory is created with mode 0700 and the database is set to
  mode 0600 where the platform permits.
- The setup handoff file defaults to
  `$HOME/.config/amiki/uosa-setup-code` (mode 0600); override with an absolute
  `AMIKI_UOSA_SETUP_CODE_FILE`.
- Back up the database using SQLite's online backup mechanism, not by copying
  a live database file without coordinating with SQLite.
- Protect backups with the same permissions and access controls as the live DB.
- Never log session bearer values, setup codes, recovery codes, raw signatures,
  or private keys. Audit events contain bounded metadata and hashes, not bearer
  values or recovery plaintext.

## Network and deployment requirements

The current Amiki API startup is designed for a local Termux loopback service.
Do not bind it to a public interface. A hosted deployment requires TLS,
trusted reverse-proxy configuration, network-level rate limiting, secret
management, monitoring, backup/restore drills, alerting, and security review
before it is production-deployed. Do not infer public-hosting readiness from
green unit tests.

## Explicit limitations / release gates

- L3 and Cipher X authorization federation are not implemented by this UOSA
  release.
- Android UI migration, physical biometric tests, hardware-backed key tests,
  lost-device/recovery UX, and API end-to-end validation on the actual phone
  remain separate release gates.
- The current SQLite and loopback assumptions are for this local-device
  deployment, not a multi-node hosted service. A distributed deployment would
  need shared transactional storage and centrally coordinated revocation and
  rate limiting.
