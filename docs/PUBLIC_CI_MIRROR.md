# Public backend CI mirror

This repository is a public CI mirror of the private Amiki backend.

## Source of truth

- Private source repository: `leoperish54-hiro/Amiki-`
- Public CI mirror: `leoperish54-hiro/amiki-backend-`

The private repository remains authoritative. The public mirror is a reviewed source snapshot for build and test automation, not the canonical development repository.

## Publication boundaries

- `config.json` contains neutral defaults only. Personal identity, academic records, and local runtime configuration are intentionally excluded.
- Runtime memory, databases, local logs, API tokens, owner-review tokens, Cipher credentials, and Cloud credentials must never be committed.
- Historical backups, working copies, and private Git history are excluded.
- CI has read-only repository permissions and does not publish releases or deployment artifacts.

## CI

The workflow installs API dependencies, compiles application modules, and runs a focused API/security regression set. Expand the test matrix only after each added test's fixtures and dependencies have been reviewed.
