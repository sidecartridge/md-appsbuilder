# Multidevice Apps Builder

Rebuilds the SideCartridge Multidevice app catalogs. The script aggregates per-app `*.json` files in the `atarist.sidecartridge.com` S3 bucket, derives each app's `previous_versions` from the `{uuid}-*.uf2` binaries it finds in the same bucket (md5 computed from the actual bytes), then writes **two** catalogs:

- `apps.json` — every app from the bucket.
- `apps-beta.json` — only apps whose current top-level `version` contains `alpha` or `beta` (case-insensitive substring). Useful for a "prerelease" channel in clients. `previous_versions` inside each included app is **not** filtered — the full history is preserved.

Each catalog is published to its own S3 key independently, and both honour the same dry-run / test / publish semantics.

## Requirements

- Python 3.10
- `pip install boto3 packaging`
- AWS credentials with read/write access to the bucket exported as `AWS_ACCESS_KEY_ID` and `AWS_SECRET_ACCESS_KEY` (CI provides these via GitHub Actions secrets).

## Usage

```bash
python updateapps.py [--test] [--publish]
```

Defaults are safe: nothing is uploaded unless you pass `--publish`.

| Command | Local files | S3 uploads |
|---|---|---|
| `python updateapps.py` | rewrites `apps.json` and `apps-beta.json` | none (dry run) |
| `python updateapps.py --publish` | rewrites `apps.json` and `apps-beta.json` | uploads each catalog **only if** its own diff shows a new UUID or a top-level `version` bump (existing objects backed up to `{key}.DDMMYYYY.bak` first) |
| `python updateapps.py --test` | rewrites `apps-test.json` and `apps-beta-test.json` | none (dry run) |
| `python updateapps.py --test --publish` | rewrites `apps-test.json` and `apps-beta-test.json` | always uploads both (no diff gate) — production `apps.json` / `apps-beta.json` untouched |

Use `--test` while iterating: it routes both local files and both S3 objects to the `*-test.json` variants, so you can inspect the results at `https://atarist.sidecartridge.com/apps-test.json` and `https://atarist.sidecartridge.com/apps-beta-test.json` without affecting the live catalogs.

## CI

- `.github/workflows/build.yml` — runs on every PR in **dry-run** mode (no upload). Useful for catching script errors before merge.
- `.github/workflows/nightly.yml` — runs daily at 06:00 UTC with `--publish`. This is the only path that writes to production `apps.json` / `apps-beta.json`.

## Output schema

Each app object in `apps.json` (and `apps-beta.json`):

```json
{
  "uuid": "...",
  "name": "...",
  "description": "... (may contain HTML <a> tags)",
  "image": "https://...",
  "tags": ["..."],
  "devices": ["..."],
  "binary": "https://atarist.sidecartridge.com/{uuid}-{version}.uf2",
  "md5": "...",
  "version": "v1.2.3",
  "previous_versions": [
    { "version": "v1.2.2", "binary": "https://...", "md5": "..." }
  ]
}
```

`previous_versions` is sorted newest-first using PEP 440 ordering and excludes the current `version`.
