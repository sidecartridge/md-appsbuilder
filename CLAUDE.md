# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Purpose

A one-shot Python script (`updateapps.py`) that rebuilds **two** catalogs for the SideCartridge Multidevice project by aggregating per-app JSON files from the `atarist.sidecartridge.com` S3 bucket, deriving each app's `previous_versions` from the `{uuid}-*.uf2` binaries it finds in the same bucket, then optionally re-uploading the results:

- `apps.json` — every app.
- `apps-beta.json` — subset whose current top-level `version` contains `alpha` or `beta` (case-insensitive substring). `previous_versions` is preserved verbatim for each included app.

## Commands

```bash
# Local runs (requires AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY in env)
python updateapps.py                  # dry run: rewrites local apps.json + apps-beta.json only
python updateapps.py --publish        # publishes each catalog IF its own diff shows a new UUID or top-level version bump
python updateapps.py --test           # dry run targeting apps-test.json + apps-beta-test.json (local only)
python updateapps.py --test --publish # always uploads both *-test.json variants (no diff gate); production keys untouched

# Dependencies (no requirements.txt — keep workflows in sync if imports change)
pip install boto3 packaging
```

CI uses Python 3.10 (`.github/workflows/*.yml`). `.python-version` names a pyenv virtualenv (`md-appsbuilder`), not a Python version.

Triggers:
- `build.yml` — pull_request + manual dispatch, **dry-run** (no `--publish`). PR-time sanity check.
- `nightly.yml` — daily 06:00 UTC, runs with `--publish`. Only path that writes production `apps.json` / `apps-beta.json`.

There are no tests or linters configured.

## Architecture

The flow in `main()` is the whole program — a shared enrich step feeding a per-catalog publish step, all against a single bucket (`atarist.sidecartridge.com`, region `us-east-1`):

1. **Aggregate** — `aggregate_json_files_from_s3` lists every `*.json` in the bucket whose key does **not** start with `apps` (excludes `apps.json`, `apps-beta.json`, their `-test` variants, and dated `.bak` files) and merges them into `{"apps": [...]}`. Paginated via `ContinuationToken`.
2. **Enrich** — `build_previous_versions` runs per app: lists `{uuid}-*.uf2` keys, downloads each object to compute md5 (never trusts `ETag` because multipart uploads emit `<hash>-<partcount>`), excludes the entry matching the current `version`, sorts newest-first via `packaging.version.parse`, and writes the result onto `app["previous_versions"]`. This **overrides** any `previous_versions` field present in the per-app JSON.
3. **Per-catalog publish** — `process_catalog` is called twice:
   - Once for the full `current_apps` list → target key `apps.json` (or `apps-test.json` with `--test`).
   - Once for the filtered list `[app for app in current_apps if is_prerelease_version(app["version"])]` → target key `apps-beta.json` (or `apps-beta-test.json`).
   Each call fetches its own S3 baseline via `fetch_remote_apps_json`, diffs with `find_new_apps_by_uuid` + `find_updated_apps_by_version`, writes the local file, and gates upload on `(new or updated) or force_upload`. `force_upload` is wired to `--test`; upload itself requires `--publish`. `backup_and_upload` copies the existing remote object to `{key}.DDMMYYYY.bak` before overwriting.

### Invariants worth preserving

- **Identity is `uuid`, comparison is top-level `version`.** Don't switch diffing to `name` or string compare — `compare_versions` uses PEP 440 specifically to handle prerelease suffixes (`v1.0.5alpha` < `v1.0.5`).
- **Exclude prefix in aggregation is `"apps"`, not `"apps.json"`.** This keeps dated `.bak` files **and** every `apps*.json` output (production, `-test`, `-beta`, `-beta-test`) out of the catalog. Changing it would let those pollute aggregation, and the beta fan-out would feed itself on the next run.
- **`process_catalog` uses the same string as both local filename and remote key.** The per-catalog local file lives in the working directory under the exact key name. Keep this coupling — CI and `.gitignore` assume it.
- **Beta filter looks only at top-level `version`**, via `is_prerelease_version` (substring `alpha`/`beta`, case-insensitive). `previous_versions` is **not** filtered inside beta apps — the full history travels with the app object.
- **`build_previous_versions` always downloads**, never falls back to ETag. The `.uf2` files are small but if upload modes change, keep this conservative — multipart ETags would silently corrupt md5 fields.
- **Filename convention `{uuid}-{version}.uf2`** is what `build_previous_versions` parses. Any historical binary not following this pattern in the bucket is invisible to the script.
- **Backup key format `{remote_key}.DDMMYYYY.bak`** (European day-first). Same-day reruns overwrite the day's backup — acceptable because each catalog is idempotent when there are no diffs.
- `parse_links` rewrites `<a href="…">label</a>` → `[label](…)` **only for console output** of new/updated entries; stored JSON keeps the original HTML in `description`.
