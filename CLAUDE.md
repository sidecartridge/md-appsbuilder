# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Purpose

A one-shot Python script (`updateapps.py`) that rebuilds the `apps.json` catalog for the SideCartridge Multidevice project by aggregating per-app JSON files from the `atarist.sidecartridge.com` S3 bucket, deriving each app's `previous_versions` from the `{uuid}-*.uf2` binaries it finds in the same bucket, then optionally re-uploading the result.

## Commands

```bash
# Local runs (requires AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY in env)
python updateapps.py                  # dry run: rewrites local apps.json only
python updateapps.py --publish        # publishes to apps.json IF a UUID is new or top-level version bumped
python updateapps.py --test           # dry run targeting apps-test.json (local only)
python updateapps.py --test --publish # always uploads to apps-test.json (no diff gate); production apps.json untouched

# Dependencies (no requirements.txt — keep workflows in sync if imports change)
pip install boto3 packaging
```

CI uses Python 3.10 (`.github/workflows/*.yml`). `.python-version` names a pyenv virtualenv (`md-appsbuilder`), not a Python version.

Triggers:
- `build.yml` — pull_request + manual dispatch, **dry-run** (no `--publish`). PR-time sanity check.
- `nightly.yml` — daily 06:00 UTC, runs with `--publish`. Only path that writes production `apps.json`.

There are no tests or linters configured.

## Architecture

The flow in `main()` is the whole program — four phases against a single bucket (`atarist.sidecartridge.com`, region `us-east-1`):

1. **Baseline** — `fetch_remote_apps_json` reads the current `apps.json` from S3 (treated as empty on `NoSuchKey`). Used as the diff baseline.
2. **Aggregate** — `aggregate_json_files_from_s3` lists every `*.json` in the bucket whose key does **not** start with `apps` (excludes `apps.json`, `apps-test.json`, and dated `.bak` files) and merges them into `{"apps": [...]}`. Paginated via `ContinuationToken`.
3. **Enrich** — `build_previous_versions` is called per app: lists `{uuid}-*.uf2` keys, downloads each object to compute md5 (never trusts `ETag` because multipart uploads emit `<hash>-<partcount>`), excludes the entry matching the current `version`, sorts newest-first via `packaging.version.parse`, and writes the result onto `app["previous_versions"]`. This **overrides** any `previous_versions` field present in the per-app JSON.
4. **Diff & publish** — `find_new_apps_by_uuid` detects additions; `find_updated_apps_by_version` detects top-level `version` bumps. The upload gate is `(new or updated) or args.test` — `--test` short-circuits the no-change check. Upload itself requires `--publish`; without it the script logs a `DRY RUN:` line. `backup_and_upload` copies the existing remote object to `{key}.DDMMYYYY.bak` before overwriting.

### Invariants worth preserving

- **Identity is `uuid`, comparison is top-level `version`.** Don't switch diffing to `name` or string compare — `compare_versions` uses PEP 440 specifically to handle prerelease suffixes (`v1.0.5alpha` < `v1.0.5`).
- **Exclude prefix in aggregation is `"apps"`, not `"apps.json"`.** This keeps dated `.bak` files *and* `apps-test.json` out of the catalog. Changing it would let those pollute aggregation.
- **`build_previous_versions` always downloads**, never falls back to ETag. The `.uf2` files are small but if upload modes change, keep this conservative — multipart ETags would silently corrupt md5 fields.
- **Filename convention `{uuid}-{version}.uf2`** is what `build_previous_versions` parses. Any historical binary not following this pattern in the bucket is invisible to the script.
- **Backup key format `{remote_key}.DDMMYYYY.bak`** (European day-first). Same-day reruns overwrite the day's backup — acceptable because the script is idempotent when there are no diffs.
- `parse_links` rewrites `<a href="…">label</a>` → `[label](…)` **only for console output** of new/updated entries; stored JSON keeps the original HTML in `description`.
