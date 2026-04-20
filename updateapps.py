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

        if response.get("IsTruncated"):
            continuation_token = response.get("NextContinuationToken")
        else:
            break

    return aggregated


def compare_versions(v1: str, v2: str) -> bool:
    return parse_version(v2) > parse_version(v1)


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
        print("No existing remote apps.json to backup.")
    except (BotoCoreError, ClientError) as e:
        print(f"Error creating backup: {e}")

    try:
        with open(local_file, 'rb') as f:
            s3_client.put_object(Bucket=bucket, Key=remote_key, Body=f)
        print(f"Uploaded new {remote_key}")
    except (BotoCoreError, ClientError, IOError) as e:
        print(f"Error uploading new apps.json: {e}")


def main():
    parser = argparse.ArgumentParser(description="Rebuild apps.json from per-app JSON files in S3.")
    parser.add_argument(
        "--publish",
        action="store_true",
        help="Upload the rebuilt JSON to S3 (default: dry run, write local file only).",
    )
    parser.add_argument(
        "--test",
        action="store_true",
        help="Target apps-test.json instead of apps.json (locally and remote) and bypass the no-change gate.",
    )
    args = parser.parse_args()

    BUCKET = 'atarist.sidecartridge.com'
    LOCAL_FILE = 'apps-test.json' if args.test else 'apps.json'
    REMOTE_KEY = 'apps-test.json' if args.test else 'apps.json'

    s3 = boto3.client('s3', region_name='us-east-1')

    # Fetch remote baseline
    remote_data = fetch_remote_apps_json(s3, BUCKET, REMOTE_KEY)
    old_apps = remote_data.get('apps', [])

    # Aggregate current bucket
    current_data = aggregate_json_files_from_s3(BUCKET)
    current_apps = current_data['apps']

    # Enrich each app with historical binaries discovered in the bucket
    for app in current_apps:
        uuid = app.get("uuid")
        if not uuid:
            continue
        previous = build_previous_versions(s3, BUCKET, app)
        app["previous_versions"] = previous
        print(f"App '{app.get('name')}' (UUID: {uuid}) — {len(previous)} previous version(s)")

    # Write local apps.json
    with open(LOCAL_FILE, 'w', encoding='utf-8') as f:
        json.dump(current_data, f, ensure_ascii=False, indent=2)
    print(f"Updated local {LOCAL_FILE}")

    # Check for new and updated entries
    new_apps = find_new_apps_by_uuid(old_apps, current_apps)
    updated_apps = find_updated_apps_by_version(old_apps, current_apps)

    if new_apps:
        print("New JSON entries detected (by UUID):")
        for app in new_apps:
            desc = parse_links(app.get('description', ''))
            print(f"- uuid='{app.get('uuid')}', name='{app.get('name')}', description='{desc}'")
    else:
        print("No new JSON entries found by UUID.")

    if updated_apps:
        print("Updated JSON entries detected (version bump):")
        for app in updated_apps:
            desc = parse_links(app.get('description', ''))
            print(f"- uuid='{app.get('uuid')}', name='{app.get('name')}', version='{app.get('version')}', description='{desc}'")
    else:
        print("No updated JSON entries found by version.")

    # Backup & upload if needed (test mode bypasses the no-change gate)
    should_upload = bool(new_apps or updated_apps) or args.test
    if should_upload:
        if args.publish:
            backup_and_upload(s3, BUCKET, LOCAL_FILE, REMOTE_KEY)
        else:
            reason = "test mode" if args.test else "changes detected"
            print(f"DRY RUN: {reason} but skipping upload. Re-run with --publish to upload to s3://{BUCKET}/{REMOTE_KEY}.")
    else:
        print(f"No changes to push to s3://{BUCKET}/{REMOTE_KEY}.")

if __name__ == '__main__':
    main()
