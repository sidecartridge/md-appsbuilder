# Multidevice Apps Builder

Rebuilds the `apps.json` catalog for the SideCartridge Multidevice project. The script aggregates per-app `*.json` files in the `atarist.sidecartridge.com` S3 bucket, derives each app's `previous_versions` from the `{uuid}-*.uf2` binaries it finds in the same bucket (md5 computed from the actual bytes), then optionally publishes the result back to S3.

## Requirements

- Python 3.10
- `pip install boto3 packaging`
- AWS credentials with read/write access to the bucket exported as `AWS_ACCESS_KEY_ID` and `AWS_SECRET_ACCESS_KEY` (CI provides these via GitHub Actions secrets).

## Usage

```bash
python updateapps.py [--test] [--publish]
```

Defaults are safe: nothing is uploaded unless you pass `--publish`.

| Command | Local file | S3 upload |
|---|---|---|
| `python updateapps.py` | rewrites `apps.json` | none (dry run) |
| `python updateapps.py --publish` | rewrites `apps.json` | uploads to `apps.json` **only if** a UUID is new or a top-level `version` bumped (existing object backed up to `apps.json.DDMMYYYY.bak` first) |
| `python updateapps.py --test` | rewrites `apps-test.json` | none (dry run) |
| `python updateapps.py --test --publish` | rewrites `apps-test.json` | always uploads to `apps-test.json` (no diff gate) — production `apps.json` untouched |

Use `--test` while iterating on the catalog or the script itself: it routes both the local output and the S3 object to `apps-test.json`, so you can inspect the result at `https://atarist.sidecartridge.com/apps-test.json` without affecting the live catalog.

## CI

- `.github/workflows/build.yml` — runs on every PR in **dry-run** mode (no upload). Useful for catching script errors before merge.
- `.github/workflows/nightly.yml` — runs daily at 06:00 UTC with `--publish`. This is the only path that writes to production `apps.json`.

## Output schema

Each app object in `apps.json`:

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
