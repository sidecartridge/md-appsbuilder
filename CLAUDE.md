# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Purpose

A one-shot Python script (`updateapps.py`) that rebuilds **two** catalogs for the SideCartridge Multidevice project by aggregating per-app JSON files from the `atarist.sidecartridge.com` S3 bucket, deriving each app's `previous_versions` from the `{uuid}-*.uf2` binaries it finds in the same bucket, then optionally re-uploading the results:

- `apps.json` — every app.
- `apps-beta.json` — subset whose current top-level `version` contains `alpha` or `beta` (case-insensitive substring). `previous_versions` is preserved verbatim for each included app.

## Commands

```bash
# Local runs (requires AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY in env)
# `source secrets.sh` exports them locally — gitignored, never commit it.
python updateapps.py                  # dry run: rewrites local apps.json + apps-beta.json only
python updateapps.py --publish        # publishes each catalog IF its own diff shows a new/removed UUID or top-level version bump
python updateapps.py --test           # dry run targeting apps-test.json + apps-beta-test.json (local only)
python updateapps.py --test --publish # always uploads both *-test.json variants (no diff gate); production keys untouched
python updateapps.py --publish --force # always uploads both production catalogs (no diff gate)

# Dependencies (no requirements.txt — keep workflows in sync if imports change)
pip install boto3 packaging
```

CI uses Python 3.10 (`.github/workflows/*.yml`). `.python-version` names a pyenv virtualenv (`md-appsbuilder`), not a Python version.

Triggers:
- `build.yml` — pull_request + manual dispatch, **dry-run** (no `--publish`). PR-time sanity check.
- `nightly.yml` — daily 06:00 UTC, runs with `--publish`. Only path that writes production `apps.json` / `apps-beta.json`.

There are no tests or linters configured. Run from the repo root — `taxonomies.json` and every output file use paths relative to the working directory.

## Architecture

The flow in `main()` is the whole program — a shared enrich step feeding a per-catalog publish step, all against a single bucket (`atarist.sidecartridge.com`, region `us-east-1`):

1. **Aggregate** — `aggregate_json_files_from_s3` lists every `*.json` in the bucket whose key does **not** start with `apps` (excludes `apps.json`, `apps-beta.json`, their `-test` variants, and dated `.bak` files) and merges them into `{"apps": [...]}`. Paginated via `ContinuationToken`. If any of those files can't be read or parsed, it lists them all and exits non-zero before anything is written or published.
2. **Enrich** — two per-app steps:
   - `normalize_taxonomies` rewrites `app["tags"]` and `app["devices"]` to canonical values using the alias maps in `taxonomies.json` (loaded once via `load_taxonomy_aliases`). Matching is case-insensitive, unknown terms pass through unchanged, and results are deduplicated preserving order. Runs for **every** app on every build (the catalog is rebuilt from scratch each run, so this covers all adds/updates).
   - `build_previous_versions` lists `{uuid}-*.uf2` keys, downloads each object to compute md5 (never trusts `ETag` because multipart uploads emit `<hash>-<partcount>`), excludes the entry matching the current `version`, sorts newest-first via `packaging.version.parse`, and writes the result onto `app["previous_versions"]`. This **overrides** any `previous_versions` field present in the per-app JSON.
3. **Per-catalog publish** — `process_catalog` is called twice:
   - Once for the full `current_apps` list → target key `apps.json` (or `apps-test.json` with `--test`).
   - Once for the filtered list `[app for app in current_apps if is_prerelease_version(app["version"])]` → target key `apps-beta.json` (or `apps-beta-test.json`).
   Each call fetches its own S3 baseline via `fetch_remote_apps_json`, diffs with `find_new_apps_by_uuid` + `find_updated_apps_by_version` + `find_removed_apps_by_uuid`, writes the local file, and gates upload on `(new or updated or removed) or force_upload`. `force_upload` is set by `--test` or `--force`; upload itself requires `--publish`. `backup_and_upload` copies the existing remote object to `{key}.DDMMYYYY.bak` before overwriting.

### What the publish gate does and doesn't see

A production upload happens only for a **new UUID**, a **removed UUID**, or a **top-level `version` increase** against the remote baseline. A removed UUID covers both an app deleted from the bucket and one moving from beta to stable, which drops out of `apps-beta.json`. None of these trigger an upload on their own; they reach S3 only when something else in the same catalog does:
- Edits to `taxonomies.json`, or to an app's `description`/`tags`/`devices`/`image`/`binary`/`md5`.
- A newly uploaded historical `.uf2` (changes `previous_versions` only).
- A version downgrade.

To push those changes, run `--publish --force`, which skips the gate for both production catalogs. `--test --publish` also skips it, but only for the `-test` keys. The nightly job never passes `--force`.

If `fetch_remote_apps_json` can't read the baseline (missing key, S3 error, bad JSON), it returns an empty list. Every app then counts as new and the catalog publishes.

### Failure behavior

- **Exits non-zero, so CI goes red:** any per-app JSON that can't be loaded (aggregation, before anything is written), or a failed `put_object`. A failed upload of `apps.json` means `apps-beta.json` is not attempted in that run.
- **Printed and ignored:** a failed backup (the upload still goes ahead), an unreadable remote baseline, and a `.uf2` that fails to download.
- **Unparseable versions:** `compare_versions` falls back to string inequality for a top-level `version` that isn't PEP 440 (e.g. `latest`), so any change counts as an update. `build_previous_versions` sorts unparseable versions last.

Top-level `binary` and `md5` are copied from the per-app JSON unchecked. Only `previous_versions` is computed from bucket contents.

### Invariants worth preserving

- **Identity is `uuid`, comparison is top-level `version`.** Don't switch diffing to `name` or string compare — `compare_versions` uses PEP 440 specifically to handle prerelease suffixes (`v1.0.5alpha` < `v1.0.5`). String comparison is only the fallback for versions PEP 440 can't parse.
- **Exclude prefix in aggregation is `"apps"`, not `"apps.json"`.** This keeps dated `.bak` files **and** every `apps*.json` output (production, `-test`, `-beta`, `-beta-test`) out of the catalog. Changing it would let those pollute aggregation, and the beta fan-out would feed itself on the next run.
- **`process_catalog` uses the same string as both local filename and remote key.** The per-catalog local file lives in the working directory under the exact key name. Keep this coupling — CI and `.gitignore` (which lists all four output names) assume it.
- **Beta filter looks only at top-level `version`**, via `is_prerelease_version` (substring `alpha`/`beta`, case-insensitive). `previous_versions` is **not** filtered inside beta apps — the full history travels with the app object.
- **`build_previous_versions` always downloads**, never falls back to ETag. The `.uf2` files are small but if upload modes change, keep this conservative — multipart ETags would silently corrupt md5 fields.
- **Filename convention `{uuid}-{version}.uf2`** is what `build_previous_versions` parses. Any historical binary not following this pattern in the bucket is invisible to the script.
- **`taxonomies.json` holds separate `tags` and `devices` maps** (canonical → list of aliases). The canonical key matches itself, so listing pure case variants as aliases is redundant — only add genuinely different spellings/words. Keep the two maps separate: a device alias must not rewrite a tag (e.g. `ST`). The file is read from the working directory; CI relies on it being committed at repo root. A missing/invalid file degrades to a pass-through (normalization disabled), never an abort.
- **Backup key format `{remote_key}.DDMMYYYY.bak`** (European day-first). Same-day reruns overwrite the day's backup — acceptable because each catalog is idempotent when there are no diffs.
- `parse_links` rewrites `<a href="…">label</a>` → `[label](…)` **only for console output** of new/updated entries; stored JSON keeps the original HTML in `description`.

---

## Working style

These behavioral guidelines bias toward caution over speed. For trivial tasks, use judgment.

### 1. Think before coding

Before implementing:
- State your assumptions explicitly. If uncertain, ask.
- If multiple interpretations exist, present them — don't pick silently.
- If a simpler approach exists, say so. Push back when warranted.
- If something is unclear, stop. Name what's confusing. Ask.

### 2. Simplicity first

Minimum code that solves the problem. Nothing speculative.
- No features beyond what was asked.
- No abstractions for single-use code.
- No "flexibility" or "configurability" that wasn't requested.
- No error handling for impossible scenarios.
- If you write 200 lines and it could be 50, rewrite it.

Ask: "Would a senior engineer say this is overcomplicated?" If yes, simplify.

### 3. Surgical changes

Touch only what you must. Clean up only your own mess.
- Don't "improve" adjacent code, comments, or formatting.
- Don't refactor things that aren't broken.
- Match existing style, even if you'd do it differently.
- If you notice unrelated dead code, mention it — don't delete it.
- When your changes orphan an import/variable/function, remove it. Don't remove pre-existing dead code unless asked.

The test: every changed line should trace directly to the user's request.

### 4. Goal-driven execution

Define success criteria. Loop until verified.
- "Add validation" → "Write tests for invalid inputs, then make them pass"
- "Fix the bug" → "Write a test that reproduces it, then make it pass"
- "Refactor X" → "Ensure tests pass before and after"

For multi-step tasks, state a brief plan with a verification check per step.

### 5. No AI attribution

Never add AI-tool attribution to commits, PR descriptions, code comments,
docs, or any other artifact. This means **no**:
- "Generated with Claude Code", "Co-authored by Claude", "Made with ChatGPT",
  or any similar phrasing.
- `Co-Authored-By: Claude …`, `Co-Authored-By: ChatGPT …`, or any other
  AI co-author trailer.
- "AI-assisted", "written with the help of an LLM", etc., as comments or
  changelog entries.

Write the message as the human author. Do not mention AI tools used to
produce the work.
