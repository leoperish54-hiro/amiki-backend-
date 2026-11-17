# UOSA session lifecycle: privileged sessions and persistent grants

## Accepted policy

- Cipher, ALE, and Amiki Cloud scopes use short-lived privileged sessions (900 seconds).
- Chat, agents, and other ordinary capability scopes use a persistent, device-bound authorization grant.
- Existing transaction-bound L2 authorization remains mandatory for L2 scopes, including `agents.run`; persistent authorization must never silently promote a request to L2.
- Device revocation, owner recovery, key rotation that invalidates the continuity key, explicit logout/revocation, or a security response revokes the persistent grant.
- A request containing scopes from both session classes is rejected. Android requests the classes separately.

## Credential model

A persistent grant is not an indefinitely valid bearer access token.

1. The owner device has its existing P-256 signing key, protected by strong biometric authentication, for enrollment, privileged sessions, and L2 transaction approval.
2. The device also has a separate P-256 continuity key in Android Keystore. It must be hardware-backed and non-exportable, but is not biometric-gated so routine renewal can happen without interrupting Chat or ordinary capability use.
3. The backend stores only the continuity public key and a hash/identifier for the persistent grant. The continuity private key never leaves Android Keystore.
4. A grant produces short-lived general access tokens. The app silently renews them by requesting a one-use refresh challenge and signing its exact message with the continuity key.
5. Refresh challenges are bound to grant ID, device ID, enrollment generation, audience, scope set, key fingerprint, nonce, and expiry. Challenges are single-use and rate-limited.
6. A persistent grant contains only the general scope class. It cannot mint Cipher/ALE/Cloud privileged sessions or L2 elevated grants.
7. Privileged sessions remain 900 seconds and are created only through the biometric owner signing key. They are not refreshable through the continuity key.

## Scope classification

The privileged class is explicit and deny-by-default: `cipher.inspect`, every `ale.*` scope, and every `cloud.*` scope. All other currently registered scopes are general by default only after tests verify their policy. Unknown scopes remain rejected by the existing allow-list.

L2 is an independent authorization dimension. For example, `agents.run` can belong to the general session class while remaining L2 and requiring a fresh, single-use, transaction-bound biometric elevation for each protected operation.

## Required backend tests

- Exact classification of every allowed scope; mixed-class requests rejected.
- Privileged access token expiry is exactly 900 seconds.
- General access token renewal works after the access token expires without a biometric prompt, using only the registered continuity public key.
- Wrong key, wrong grant, wrong device, wrong generation, changed scopes, expired challenge, replayed challenge, revoked grant, and revoked device all fail closed.
- Recovery revokes all grants and sessions, and old continuity keys cannot renew.
- General grants cannot mint privileged scopes or L2 authority.
- `agents.run` remains L2 and transaction-bound despite belonging to the general session class.
- Logout/revocation invalidates the grant, and refresh issuance is rate-limited.
- No private key, raw refresh secret, recovery code, or access token is written to audit events.

## Android requirements

- Store the general access token and grant identifier in the existing encrypted Keystore-backed vault; never persist raw private-key material.
- Generate and validate the continuity key separately from the biometric owner key. Reject persistent-grant setup if Android Keystore does not provide the required hardware-backed key.
- Renew in the background only when needed; serialize refresh attempts and clear stale local grants after server rejection.
- Keep the privileged session and general access token in separate vault entries.
- Recovery must rotate the owner signing key and continuity key, clear all local sessions/grants, and display replacement recovery codes only once.

## Delivery order

1. Backend scope policy, schema migration, grant/refresh endpoints, and security tests.
2. Public backend mirror CI green on the exact feature branch.
3. Android vault, continuity-key lifecycle, split scope requests, and recovery UI.
4. Public Android mirror CI green, then HOT 9 physical-device acceptance.
5. Only after both sides pass: merge reviewed changes into the private canonical repositories.
