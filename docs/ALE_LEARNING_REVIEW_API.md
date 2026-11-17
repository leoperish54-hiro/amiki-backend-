# ALE Learning Review API

The Learning Review API exposes pending learning candidates to the owner and
provides explicit approval/rejection controls. It is separate from research
ingestion: research results remain pending and are never promoted automatically.

## Security model

All review endpoints require an owner bearer token supplied through the
`AMIKI_OWNER_REVIEW_TOKEN` environment variable.

- The token must be at least 32 characters.
- The API reads the token at request time; it is not written to `config.json`.
- Missing or weak configuration disables review operations with HTTP 503.
- Missing or incorrect authorization returns HTTP 401.
- Comparisons use constant-time `secrets.compare_digest`.
- Never commit the token, put it in application logs, or expose it to the Android
  client as a hard-coded value.
- Use HTTPS whenever review endpoints are reachable beyond a trusted local device.
- This token protects the review endpoints only; it does not authenticate the
  rest of the existing API.

Generate a token locally with a cryptographically secure generator, for example:

```sh
python -c 'import secrets; print(secrets.token_urlsafe(48))'
```

On Termux, the API startup script can load it from a private file
outside the repository. Generate and store one locally:

```sh
mkdir -p "$HOME/.config/amiki"
umask 077
python -c 'import secrets; print(secrets.token_urlsafe(48))' > "$HOME/.config/amiki/owner-review-token"
chmod 600 "$HOME/.config/amiki/owner-review-token"
```

The default startup-script path is
`$HOME/.config/amiki/owner-review-token`. To use another path, set
`AMIKI_OWNER_REVIEW_TOKEN_FILE` before launching the API. An explicitly exported
`AMIKI_OWNER_REVIEW_TOKEN` takes precedence over the file. Restart the API process
after creating or changing the token file so the new environment is loaded.

Copy the token from that private file into the Android Learning Review screen.
Do not paste it into chat, commit it, or store it in the repository.

## Endpoints

All endpoints are under `/api/v1` and require:

```
Authorization: Bearer <owner-token>
```

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

## Trust boundaries and limitations

The API does not verify the truth of a research candidate. The owner must inspect
its source and evidence before approval. External snippets remain untrusted data.
This API does not update model weights or perform fine-tuning.

The bearer token is a narrow first owner gate, not a complete authentication
system for the wider Amiki API. Network exposure, HTTPS termination, token
rotation, audit logging, and Android-side secure credential storage remain
separate deployment/security tasks.
