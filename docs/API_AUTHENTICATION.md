# Core API authentication

All Core API routes except `GET /api/v1/health` require a dedicated bearer token.

## Credential separation

The Core API token is distinct from:
- `AMIKI_OWNER_REVIEW_TOKEN` (ALE owner review and restricted learning operations)
- `AMIKI_CLOUD_API_TOKEN` (Core-to-Cloud service authentication)
- `AMIKI_READ_TOKEN` / `CIPHER_API_TOKEN` (Core-to-Cipher service authentication)

Do not reuse one credential for another boundary.

## Termux provisioning

Create a strong token and store it in the private Termux configuration directory:

```bash
mkdir -p "$HOME/.config/amiki"
python - <<'PY'
from pathlib import Path
import secrets

path = Path.home() / ".config" / "amiki" / "api-token"
token = secrets.token_urlsafe(48)
path.parent.mkdir(parents=True, exist_ok=True)
path.write_text(token + "\n", encoding="utf-8")
path.chmod(0o600)
print(token)
PY
```

Copy the displayed token into Amiki's **Settings → Privacy & Security → Core API Authentication** screen. The Android client must store it encrypted with Android Keystore and send it as `Authorization: Bearer <token>` on every protected Core API request. Do not paste it into chat, logs, source code, or GitHub.

Restart the Core API after provisioning. `scripts/start_api.sh` loads the token from `$HOME/.config/amiki/api-token` unless `AMIKI_API_TOKEN` is already exported. A custom file can be selected with `AMIKI_API_TOKEN_FILE`.

## Failure behavior

- Missing or shorter-than-32-character server token: protected endpoints return HTTP 503.
- Missing, malformed, or incorrect bearer token: protected endpoints return HTTP 401.
- `GET /api/v1/health` remains unauthenticated for local liveness checks.
- Conversation IDs, `approved: true` request fields, ALE owner tokens, and Cloud/Cipher service credentials are not substitutes for Core API authentication.

The token comparison uses `secrets.compare_digest`. The API token is checked on every protected request, so token rotation takes effect after the process environment is updated and the API is restarted.
