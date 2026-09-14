import argparse
import hashlib
import json
import boto3
from botocore.exceptions import BotoCoreError, ClientError
import datetime
import re

from packaging.version import parse as parse_version, InvalidVersion


def fetch_remote_apps_json(s3_client, bucket: str, key: str) -> dict:
    """
    Fetch existing apps.json from S3 bucket. Returns structure {"apps": [...]} or empty if not found.
    """
    try:
        obj = s3_client.get_object(Bucket=bucket, Key=key)
        body = obj['Body'].read().decode('utf-8')
        return json.loads(body)
    except s3_client.exceptions.NoSuchKey:
        return {"apps": []}
    except (BotoCoreError, ClientError, json.JSONDecodeError) as e:
        print(f"Warning: could not load remote {key}: {e}")
        return {"apps": []}


def aggregate_json_files_from_s3(bucket_name: str, exclude_prefix: str = "apps") -> dict:
    """
    Aggregate all .json files in bucket (excluding those starting with prefix) into structure {"apps": [...] }.
    """
    s3 = boto3.client('s3', region_name='us-east-1')
    aggregated = {"apps": []}
    failed = []
    continuation_token = None

    while True:
        list_kwargs = {"Bucket": bucket_name, "MaxKeys": 1000}
        if continuation_token:
            list_kwargs["ContinuationToken"] = continuation_token

        response = s3.list_objects_v2(**list_kwargs)
        for obj in response.get("Contents", []):
            key = obj["Key"]
            if key.lower().endswith(".json") and not key.startswith(exclude_prefix):
                try:
                    data = json.loads(
                        s3.get_object(Bucket=bucket_name, Key=key)["Body"]
                           .read().decode("utf-8")
                    )
                    aggregated["apps"].append(data)
                except Exception as e:
                    print(f"Error processing {key}: {e}")
                    failed.append(key)

        if response.get("IsTruncated"):
            continuation_token = response.get("NextContinuationToken")
        else:
            break

    # Abort rather than build a catalog missing these apps (removals trigger a publish)
    if failed:
        raise SystemExit(f"Aborting: could not load {len(failed)} app JSON file(s): {', '.join(failed)}")

    return aggregated


def compare_versions(v1: str, v2: str) -> bool:
    try:
        return parse_version(v2) > parse_version(v1)
    except InvalidVersion:
        print(f"Warning: non-PEP 440 version ({v1!r} -> {v2!r}); treating any change as an update")
        return v1 != v2


def find_new_apps_by_uuid(old_apps: list, new_apps: list) -> list:
    """
    Return list of new app objects whose 'uuid' is not present in old_apps.
    """
    old_uuids = {app.get("uuid") for app in old_apps if app.get("uuid")}
    return [app for app in new_apps if app.get("uuid") not in old_uuids]


def find_updated_apps_by_version(old_apps: list, new_apps: list) -> list:
    """
    Return list of apps whose 'uuid' exists in old_apps but have a higher 'version'.
    """
    old_versions = {app.get("uuid"): app.get("version") for app in old_apps if app.get("uuid") and app.get("version")}
    updates = []
    for app in new_apps:
        uuid = app.get("uuid")
        new_version = app.get("version")
        old_version = old_versions.get(uuid)
        print(f"Checking app {app.get('name')} (UUID: {uuid}) - Old version: {old_version}, New version: {new_version}")
        if uuid and new_version and old_version and compare_versions(old_version, new_version):
            updates.append(app)
    return updates


def find_removed_apps_by_uuid(old_apps: list, new_apps: list) -> list:
    """
    Return list of old app objects whose 'uuid' is no longer present in new_apps.
    """
    new_uuids = {app.get("uuid") for app in new_apps if app.get("uuid")}
    return [app for app in old_apps if app.get("uuid") and app.get("uuid") not in new_uuids]


def parse_links(text: str) -> str:
    """
    Convert HTML <a ... href="url" ...>label</a> to Markdown [label](url),
    handling extra attributes like target or rel.
    """
    def repl(match):
        url = match.group('url')
        label = match.group('label')
        return f"[{label}]({url})"

    # regex to find <a ... href="url" ...>label</a>
    pattern = re.compile(
        r'<a\s+[^>]*?href=[\"\'](?P<url>[^\"\']+)[\"\'][^>]*?>(?P<label>.*?)</a>',
        re.IGNORECASE | re.DOTALL
    )
    return pattern.sub(repl, text)


def build_previous_versions(s3_client, bucket: str, app: dict) -> list:
    """
    Discover historical binaries for `app` by listing `{uuid}-*.uf2` keys in the
    bucket, downloading each to compute md5 (conservative: never trusts ETag,
    which is unreliable for multipart uploads), and excluding the entry that
    matches the app's current `version`. Returns newest-first by PEP 440.
    """
    uuid = app.get("uuid")
    current_version = app.get("version")
    if not uuid:
        return []

    prefix = f"{uuid}-"
    suffix = ".uf2"
    found = []
    continuation_token = None

    while True:
        list_kwargs = {"Bucket": bucket, "Prefix": prefix, "MaxKeys": 1000}
        if continuation_token:
            list_kwargs["ContinuationToken"] = continuation_token
        resp = s3_client.list_objects_v2(**list_kwargs)

        for obj in resp.get("Contents", []):
            key = obj["Key"]
            if not key.endswith(suffix):
                continue
            version_str = key[len(prefix):-len(suffix)]
            if current_version and version_str == current_version:
                continue
            try:
                body = s3_client.get_object(Bucket=bucket, Key=key)["Body"].read()
            except (BotoCoreError, ClientError) as e:
                print(f"Error downloading {key}: {e}")
                continue
            found.append({
                "version": version_str,
                "binary": f"https://{bucket}/{key}",
                "md5": hashlib.md5(body).hexdigest(),
            })

        if resp.get("IsTruncated"):
            continuation_token = resp.get("NextContinuationToken")
        else:
            break

    def sort_key(entry):
        try:
            return (1, parse_version(entry["version"]))
        except InvalidVersion:
            return (0, entry["version"])

    found.sort(key=sort_key, reverse=True)
    return found


def is_prerelease_version(version: str) -> bool:
    """True if `version` contains 'alpha' or 'beta' as a case-insensitive substring."""
    v = (version or "").lower()
    return "alpha" in v or "beta" in v


def load_taxonomy_aliases(path: str = "taxonomies.json") -> dict:
    """
    Load canonical->aliases maps for `tags` and `devices` and invert them into
    case-insensitive alias->canonical lookups (the canonical term maps to itself).
    Missing/invalid file disables normalization rather than aborting the build.
    """
    try:
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError) as e:
        print(f"Warning: could not load {path}: {e}; skipping taxonomy normalization")
        raw = {}

    lookups = {}
    for field in ("tags", "devices"):
        lookup = {}
        for canonical, aliases in raw.get(field, {}).items():
            lookup[canonical.lower()] = canonical
            for alias in aliases:
                lookup[alias.lower()] = canonical
        lookups[field] = lookup
    return lookups


def normalize_terms(values: list, alias_map: dict) -> list:
    """
    Map each term to its canonical form via `alias_map` (case-insensitive),
    leaving unknown terms unchanged. Deduplicates while preserving first-seen order.
    """
    result, seen = [], set()
    for v in values:
        canonical = alias_map.get(v.strip().lower(), v) if isinstance(v, str) else v
        if canonical not in seen:
            seen.add(canonical)
            result.append(canonical)
    return result


def normalize_taxonomies(app: dict, lookups: dict) -> None:
    """Rewrite `app`'s tags/devices in place to their canonical taxonomy values."""
    for field in ("tags", "devices"):
        values = app.get(field)
        if isinstance(values, list):
            app[field] = normalize_terms(values, lookups[field])


def process_catalog(
    s3_client,
    bucket: str,
    apps: list,
    key: str,
    publish: bool,
    force_upload: bool,
) -> None:
    """
    Write `apps` wrapped as {"apps": [...]} to a local file named `key`, diff
    against s3://bucket/`key`, print new/updated entries, and upload with
    backup if the diff is non-empty (or force_upload is set). Upload requires
    publish=True.
    """
    data = {"apps": apps}
    with open(key, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"[{key}] Updated local file ({len(apps)} apps)")

    old_apps = fetch_remote_apps_json(s3_client, bucket, key).get("apps", [])
    new_apps = find_new_apps_by_uuid(old_apps, apps)
    updated_apps = find_updated_apps_by_version(old_apps, apps)
    removed_apps = find_removed_apps_by_uuid(old_apps, apps)

    if new_apps:
        print(f"[{key}] New entries (by UUID):")
        for app in new_apps:
            desc = parse_links(app.get("description", ""))
            print(f"  - uuid='{app.get('uuid')}', name='{app.get('name')}', description='{desc}'")
    else:
        print(f"[{key}] No new entries by UUID.")

    if updated_apps:
        print(f"[{key}] Updated entries (version bump):")
        for app in updated_apps:
            desc = parse_links(app.get("description", ""))
            print(f"  - uuid='{app.get('uuid')}', name='{app.get('name')}', version='{app.get('version')}', description='{desc}'")
    else:
        print(f"[{key}] No updated entries by version.")

    if removed_apps:
        print(f"[{key}] Removed entries (by UUID):")
        for app in removed_apps:
            print(f"  - uuid='{app.get('uuid')}', name='{app.get('name')}', version='{app.get('version')}'")
    else:
        print(f"[{key}] No removed entries by UUID.")

    should_upload = bool(new_apps or updated_apps or removed_apps) or force_upload
    if should_upload:
        if publish:
            backup_and_upload(s3_client, bucket, key, key)
        else:
            reason = "upload forced" if force_upload else "changes detected"
            print(f"[{key}] DRY RUN: {reason} but skipping upload. Re-run with --publish.")
    else:
        print(f"[{key}] No changes to push.")


def backup_and_upload(s3_client, bucket: str, local_file: str, remote_key: str) -> None:
    """
    Backup existing remote_key to remote_key.DDMMYYYY.bak then upload local_file as remote_key.
    """
    date_str = datetime.date.today().strftime("%d%m%Y")
    backup_key = f"{remote_key}.{date_str}.bak"
    try:
        s3_client.copy_object(Bucket=bucket, CopySource={'Bucket': bucket, 'Key': remote_key}, Key=backup_key)
        print(f"Created backup: {backup_key}")
    except s3_client.exceptions.NoSuchKey:
        print(f"No existing remote {remote_key} to backup.")
    except (BotoCoreError, ClientError) as e:
        print(f"Error creating backup: {e}")

    try:
        with open(local_file, 'rb') as f:
            s3_client.put_object(Bucket=bucket, Key=remote_key, Body=f)
        print(f"Uploaded new {remote_key}")
    except (BotoCoreError, ClientError, IOError) as e:
        raise SystemExit(f"Error uploading new {remote_key}: {e}")


def main():
    parser = argparse.ArgumentParser(description="Rebuild apps.json (and apps-beta.json) from per-app JSON files in S3.")
    parser.add_argument(
        "--publish",
        action="store_true",
        help="Upload the rebuilt JSON files to S3 (default: dry run, write local files only).",
    )
    parser.add_argument(
        "--test",
        action="store_true",
        help="Target apps-test.json / apps-beta-test.json instead of production keys, and bypass the no-change gate.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Bypass the no-change gate and upload every catalog even without changes (still requires --publish).",
    )
    args = parser.parse_args()

    BUCKET = "atarist.sidecartridge.com"
    main_key = "apps-test.json" if args.test else "apps.json"
    beta_key = "apps-beta-test.json" if args.test else "apps-beta.json"

    s3 = boto3.client("s3", region_name="us-east-1")

    # Aggregate current bucket
    current_apps = aggregate_json_files_from_s3(BUCKET)["apps"]

    # Canonical taxonomy lookups for tags/devices
    taxonomy_aliases = load_taxonomy_aliases()

    # Enrich each app with historical binaries discovered in the bucket
    for app in current_apps:
        normalize_taxonomies(app, taxonomy_aliases)
        uuid = app.get("uuid")
        if not uuid:
            continue
        previous = build_previous_versions(s3, BUCKET, app)
        app["previous_versions"] = previous
        print(f"App '{app.get('name')}' (UUID: {uuid}) — {len(previous)} previous version(s)")

    # Main catalog: all apps
    force_upload = args.test or args.force

    process_catalog(s3, BUCKET, current_apps, main_key, args.publish, force_upload)

    # Beta catalog: only apps whose current top-level version contains alpha/beta
    beta_apps = [app for app in current_apps if is_prerelease_version(app.get("version", ""))]
    process_catalog(s3, BUCKET, beta_apps, beta_key, args.publish, force_upload)

if __name__ == '__main__':
    main()
