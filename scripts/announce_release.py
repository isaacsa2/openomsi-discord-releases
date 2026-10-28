#!/usr/bin/env python3
"""Poll official GitHub releases, announce new versions, and update existing posts.

Standard library only. No bot, external database, or server is needed in webhook mode.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from discord_delivery import (
    DEFAULT_REPORT_CHANNEL_ID, SNOWFLAKE_RE, THREAD_MODES,
    check_discord_webhook, edit_discord, format_changelog, make_payload,
    report_template, report_thread_name, send_discord, start_report_thread,
    webhook_url_with_wait,
)

STATE_PATH = Path(__file__).resolve().parent.parent / "state" / "announced.json"
API_BASE = "https://api.github.com"
USER_AGENT = "openOMSI-Discord-Release-Announcer/3.0"
MAX_PAGES = 5
MAX_RELEASES_PER_RUN = 5
MAX_EDIT_CHECKS = 30


def parse_date(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def read_state(path: Path = STATE_PATH) -> dict:
    state = json.loads(path.read_text(encoding="utf-8"))
    parse_date(state["baseline_published_at"])
    state["announced_release_ids"] = list(map(int, state["announced_release_ids"]))
    state.setdefault("pending_threads", {})  # Backward-compatible with v2 state.
    state.setdefault("messages", {})
    return state


def get_json(url: str, token: str = ""):
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": USER_AGENT,
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = Request(url, headers=headers)
    with urlopen(request, timeout=25) as response:
        return json.load(response)


def fetch_releases(source_repo: str, token: str) -> list[dict]:
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", source_repo):
        raise ValueError("Invalid SOURCE_REPOSITORY (expected owner/repo).")
    releases = []
    for page in range(1, MAX_PAGES + 1):
        url = f"{API_BASE}/repos/{source_repo}/releases?per_page=100&page={page}"
        batch = get_json(url, token)
        if not isinstance(batch, list):
            raise ValueError("GitHub API did not return a list of releases.")
        releases.extend(batch)
        if len(batch) < 100:
            break
    return releases


def eligible_releases(releases: list[dict], state: dict, include_prereleases: bool) -> list[dict]:
    baseline = parse_date(state["baseline_published_at"])
    sent = set(state["announced_release_ids"])
    candidates = []
    for release in releases:
        published = release.get("published_at")
        if (not published or release.get("draft") or
                (release.get("prerelease") and not include_prereleases)):
            continue
        if int(release["id"]) in sent or parse_date(published) < baseline:
            continue
        candidates.append(release)
    candidates.sort(key=lambda r: (parse_date(r["published_at"]), int(r["id"])))
    return candidates[:MAX_RELEASES_PER_RUN]


def previous_tag(release: dict, releases: list[dict]) -> str:
    earlier = [r for r in releases if r.get("published_at") and not r.get("draft") and
               (parse_date(r["published_at"]), int(r["id"])) <
               (parse_date(release["published_at"]), int(release["id"]))]
    return str(max(earlier, key=lambda r: (parse_date(r["published_at"]), int(r["id"])))['tag_name']) if earlier else ""


def release_fingerprint(release: dict, previous: str = "") -> str:
    """Stable fields only: edits and new official assets trigger message updates."""
    assets = sorted((int(a.get("id") or 0), str(a.get("name") or ""),
                     str(a.get("browser_download_url") or ""), int(a.get("size") or 0))
                    for a in release.get("assets") or [])
    data = {
        "tag": str(release["tag_name"]), "name": str(release.get("name") or ""),
        "body": str(release.get("body") or ""),
        "prerelease": bool(release.get("prerelease")), "published_at": release["published_at"],
        "assets": assets, "previous_tag": previous,
    }
    return hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


def webhook_identity(webhook: str) -> str:
    # Only non-secret webhook ID is retained to guard against editing a different webhook.
    from urllib.parse import urlsplit
    webhook_url_with_wait(webhook)
    return urlsplit(webhook).path.rstrip("/").split("/")[-2]


def summary(line: str) -> None:
    """Safe message: never include token-containing URLs in this file."""
    print(line)
    path = os.getenv("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as file:
            file.write(line + "\n")


def write_and_push_state(state: dict, release: dict):
    STATE_PATH.write_text(json.dumps(state, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    subprocess.run(["git", "config", "user.name", "github-actions[bot]"], check=True)
    subprocess.run(["git", "config", "user.email", "41898282+github-actions[bot]@users.noreply.github.com"], check=True)
    subprocess.run(["git", "add", str(STATE_PATH)], check=True)
    staged = subprocess.run(["git", "diff", "--cached", "--quiet"], check=False)
    if staged.returncode == 0:
        return
    if staged.returncode != 1:
        raise RuntimeError("Unable to check staged state changes.")
    tag = str(release["tag_name"]).replace("\n", " ")[:80]
    subprocess.run(["git", "commit", "-m", f"chore: sync openOMSI {tag}"], check=True)
    branch = os.getenv("GITHUB_REF_NAME", "main")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", branch):
        raise ValueError("Invalid GITHUB_REF_NAME.")
    # Do not force-push. A concurrent commit should fail visibly rather than lose state.
    subprocess.run(["git", "push", "origin", f"HEAD:refs/heads/{branch}"], check=True)


def deliver_release(release: dict, source: str, webhook: str, thread_mode: str,
                    bot_token: str, state: dict | None = None, test: bool = False,
                    report_channel_id: str = DEFAULT_REPORT_CHANNEL_ID,
                    prior_tag: str = ""):
    """New release, with legacy optional per-release thread modes preserved."""
    key = str(release["id"])
    pending = state.setdefault("pending_threads", {}) if state is not None else {}
    progress = pending.get(key)
    payload = make_payload(release, source, test=test, thread_mode=thread_mode,
                           forum=(thread_mode == "forum"), report_channel_id=report_channel_id,
                           previous_tag=prior_tag)

    if thread_mode == "webhook":
        if progress is None:
            response = send_discord(payload, webhook)
            message_id = str(response.get("id", ""))
            if state is not None:
                if not SNOWFLAKE_RE.fullmatch(message_id):
                    raise RuntimeError("Discord did not return a valid message ID; state not marked sent.")
                state.setdefault("messages", {})[key] = {
                    "message_id": message_id,
                    "channel_id": str(response.get("channel_id", "")),
                    "webhook_id": webhook_identity(webhook),
                    "tag": str(release["tag_name"]),
                    "fingerprint": release_fingerprint(release, prior_tag),
                }
        if state is not None:
            if int(release["id"]) not in state["announced_release_ids"]:
                state["announced_release_ids"].append(int(release["id"]))
            pending.pop(key, None)
            write_and_push_state(state, release)
        return None

    if progress is None:
        message = send_discord(payload, webhook)
        message_id, channel_id = str(message.get("id", "")), str(message.get("channel_id", ""))
        if not (SNOWFLAKE_RE.fullmatch(message_id) and SNOWFLAKE_RE.fullmatch(channel_id)):
            raise RuntimeError("Discord did not return valid message/channel IDs.")
        progress = {"message_id": message_id, "channel_id": channel_id}
        if thread_mode == "forum":
            progress["thread_id"] = channel_id
        if state is not None:
            pending[key] = progress
            write_and_push_state(state, release)
    if not progress.get("thread_id"):
        progress["thread_id"] = start_report_thread(
            progress["channel_id"], progress["message_id"],
            report_thread_name(str(release["tag_name"]), test=test), bot_token)
        if state is not None:
            write_and_push_state(state, release)
    if not progress.get("report_prompt_sent"):
        send_discord(report_template(str(release["tag_name"]), str(release["html_url"]), test=test),
                     webhook, thread_id=progress["thread_id"])
        progress["report_prompt_sent"] = True
        if state is not None:
            write_and_push_state(state, release)
    if state is not None:
        state["announced_release_ids"].append(int(release["id"]))
        del pending[key]
        write_and_push_state(state, release)
    return progress["thread_id"]


def sync_updated_releases(releases: list[dict], state: dict, source: str,
                          webhook: str, report_channel_id: str) -> list[str]:
    """Patch previously sent webhook messages in place if official notes/assets changed."""
    known = state.get("messages", {})
    published = [r for r in releases if not r.get("draft") and r.get("published_at") and
                 str(r.get("id")) in known]
    published.sort(key=lambda r: parse_date(r["published_at"]), reverse=True)
    changed = []
    for release in published[:MAX_EDIT_CHECKS]:
        key = str(release["id"])
        stored = known[key]
        new_fingerprint = release_fingerprint(release, previous_tag(release, releases))
        if stored.get("fingerprint") == new_fingerprint:
            continue
        if str(stored.get("webhook_id", "")) != webhook_identity(webhook):
            raise RuntimeError("Webhook ID changed: cannot edit old posts with a different webhook.")
        message_id = str(stored.get("message_id", ""))
        payload = make_payload(release, source, thread_mode="webhook",
                               report_channel_id=report_channel_id,
                               previous_tag=previous_tag(release, releases))
        # Never repost on HTTP 404: deleted messages require manual recovery.
        edited = edit_discord(payload, webhook, message_id)
        if str(edited.get("id", "")) != message_id:
            raise RuntimeError("Discord edit did not confirm the original message ID.")
        stored.update({"fingerprint": new_fingerprint, "tag": str(release["tag_name"])})
        write_and_push_state(state, release)
        changed.append(str(release["tag_name"]))
    return changed


def main() -> int:
    mode = os.getenv("WORKFLOW_MODE", "check")
    if mode not in ("check", "preview_latest", "test_webhook", "healthcheck"):
        raise ValueError("WORKFLOW_MODE must be check, preview_latest, test_webhook, or healthcheck.")
    token, webhook = os.getenv("GITHUB_TOKEN", ""), os.getenv("DISCORD_WEBHOOK_URL", "")
    bot_token = os.getenv("DISCORD_BOT_TOKEN", "")
    thread_mode = os.getenv("DISCORD_THREAD_MODE", "webhook").lower()
    report_channel_id = os.getenv("DISCORD_REPORT_CHANNEL_ID", DEFAULT_REPORT_CHANNEL_ID)
    if thread_mode not in THREAD_MODES:
        raise ValueError("DISCORD_THREAD_MODE must be webhook, text or forum.")
    source = os.getenv("SOURCE_REPOSITORY", "openOMSI-org/openOMSI")
    include_pre = os.getenv("ANNOUNCE_PRERELEASES", "true").lower() == "true"
    edit_updates = os.getenv("EDIT_RELEASE_UPDATES", "true").lower() == "true"
    releases = fetch_releases(source, token)
    summary(f"### openOMSI Release Center • {mode}")
    summary(f"GitHub API: OK ({len(releases)} releases indexed).")

    if mode == "preview_latest":
        published = [r for r in releases if r.get("published_at") and not r.get("draft")]
        if not published:
            summary("No published releases available.")
            return 0
        latest = max(published, key=lambda r: (parse_date(r["published_at"]), int(r["id"])))
        print(json.dumps(make_payload(latest, source, forum=(thread_mode == "forum"),
                                      thread_mode=thread_mode, report_channel_id=report_channel_id,
                                      previous_tag=previous_tag(latest, releases)),
                         indent=2, ensure_ascii=False))
        return 0

    if not webhook:
        raise RuntimeError("Set DISCORD_WEBHOOK_URL as a GitHub Actions secret.")
    # A HEAD-less webhook GET catches invalid secrets even if there are no new releases.
    if mode in ("check", "healthcheck"):
        check_discord_webhook(webhook)
        summary("Discord webhook: OK (credential validated without posting).")
        if mode == "healthcheck":
            return 0

    if mode == "test_webhook":
        published = [r for r in releases if r.get("published_at") and not r.get("draft")]
        if not published:
            summary("No published releases available.")
            return 0
        latest = max(published, key=lambda r: (parse_date(r["published_at"]), int(r["id"])))
        if thread_mode == "text" and not bot_token:
            raise RuntimeError("DISCORD_BOT_TOKEN is required for text-channel threads.")
        deliver_release(latest, source, webhook, thread_mode, bot_token, test=True,
                        report_channel_id=report_channel_id, prior_tag=previous_tag(latest, releases))
        summary(f"TEST posted: {latest['tag_name']} (no state change).")
        return 0

    state = read_state()
    pending = eligible_releases(releases, state, include_pre)
    if thread_mode == "text" and pending and not bot_token:
        raise RuntimeError("DISCORD_BOT_TOKEN is required for text-channel threads.")
    for release in pending:
        deliver_release(release, source, webhook, thread_mode, bot_token, state,
                        report_channel_id=report_channel_id, prior_tag=previous_tag(release, releases))
        summary(f"New release announced: {release['tag_name']}")
    if thread_mode == "webhook" and edit_updates:
        for tag in sync_updated_releases(releases, state, source, webhook, report_channel_id):
            summary(f"Existing announcement updated: {tag}")
    if not pending:
        summary("No new releases to announce.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (ValueError, RuntimeError, HTTPError, URLError, OSError, subprocess.CalledProcessError) as error:
        if isinstance(error, (HTTPError, URLError)):
            print("GitHub API or Discord connection failed.", file=sys.stderr)
        elif isinstance(error, subprocess.CalledProcessError):
            print("State commit/push failed; check workflow permissions and branch policy.", file=sys.stderr)
        else:
            # Errors raised in this module never include webhook URLs/tokens.
            print(f"Error: {error}", file=sys.stderr)
        sys.exit(1)
