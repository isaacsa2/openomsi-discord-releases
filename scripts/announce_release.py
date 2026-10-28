#!/usr/bin/env python3
"""Poll official GitHub Releases and announce *new* versions through Discord.

Only standard-library Python is used. Never commit a Discord webhook URL.
The tracked state file is updated after a webhook confirms acceptance.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

STATE_PATH = Path(__file__).resolve().parent.parent / "state" / "announced.json"
API_BASE = "https://api.github.com"
USER_AGENT = "openOMSI-Discord-Release-Announcer/1.0"
MAX_PAGES = 5
MAX_RELEASES_PER_RUN = 5


def parse_date(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def read_state(path: Path = STATE_PATH) -> dict:
    state = json.loads(path.read_text(encoding="utf-8"))
    parse_date(state["baseline_published_at"])
    state["announced_release_ids"] = list(map(int, state["announced_release_ids"]))
    state.setdefault("pending_threads", {})
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


from discord_delivery import (
    THREAD_MODES,
    format_changelog,
    webhook_url_with_wait,
    make_payload,
    report_template,
    report_thread_name,
    send_discord,
    start_report_thread,
)


def deliver_release(release: dict, source: str, webhook: str, thread_mode: str,
                    bot_token: str, state: dict | None = None, test: bool = False):
    """Checkpoint each step so a thread failure doesn't repost the announcement."""
    key = str(release["id"])
    pending = state.setdefault("pending_threads", {}) if state is not None else {}
    progress = pending.get(key)

    if progress is None:
        message = send_discord(make_payload(release, source, test=test,
                                            forum=(thread_mode == "forum")), webhook)
        message_id = str(message.get("id", ""))
        channel_id = str(message.get("channel_id", ""))
        if not (message_id.isdigit() and channel_id.isdigit()):
            raise RuntimeError("Discord did not provide message and channel IDs; cannot create thread.")
        progress = {"message_id": message_id, "channel_id": channel_id}
        if thread_mode == "forum":
            # A forum webhook creates the thread as its post; channel_id is thread id.
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
    subprocess.run(["git", "commit", "-m", f"chore: announce openOMSI {tag}"], check=True)
    branch = os.getenv("GITHUB_REF_NAME", "main")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", branch):
        raise ValueError("Invalid GITHUB_REF_NAME.")
    subprocess.run(["git", "push", "origin", f"HEAD:refs/heads/{branch}"], check=True)


def main() -> int:
    mode = os.getenv("WORKFLOW_MODE", "check")
    if mode not in ("check", "preview_latest", "test_webhook"):
        raise ValueError("WORKFLOW_MODE must be check, preview_latest or test_webhook.")
    token = os.getenv("GITHUB_TOKEN", "")
    webhook = os.getenv("DISCORD_WEBHOOK_URL", "")
    bot_token = os.getenv("DISCORD_BOT_TOKEN", "")
    thread_mode = os.getenv("DISCORD_THREAD_MODE", "text").lower()
    if thread_mode not in THREAD_MODES:
        raise ValueError("DISCORD_THREAD_MODE must be text or forum.")
    source = os.getenv("SOURCE_REPOSITORY", "openOMSI-org/openOMSI")
    include_pre = os.getenv("ANNOUNCE_PRERELEASES", "true").lower() == "true"
    releases = fetch_releases(source, token)

    if mode in ("preview_latest", "test_webhook"):
        published = [r for r in releases if r.get("published_at") and not r.get("draft")]
        if not published:
            print("No published releases found.")
            return 0
        latest = max(published, key=lambda r: (parse_date(r["published_at"]), int(r["id"])))
        if mode == "preview_latest":
            print(json.dumps(make_payload(latest, source, forum=(thread_mode == "forum")),
                             indent=2, ensure_ascii=False))
            return 0
        if not webhook:
            raise RuntimeError("Set the DISCORD_WEBHOOK_URL Actions secret before sending a test.")
        if thread_mode == "text" and not bot_token:
            raise RuntimeError("Set DISCORD_BOT_TOKEN for text-channel threads, or select forum mode.")
        thread_id = deliver_release(latest, source, webhook, thread_mode, bot_token, test=True)
        print(f"Test announcement and bug-report thread created for {latest['tag_name']} (thread {thread_id}; state unchanged).")
        return 0

    state = read_state()
    pending = eligible_releases(releases, state, include_pre)
    if not pending:
        print("No new releases to announce.")
        return 0
    if not webhook:
        raise RuntimeError("Set the DISCORD_WEBHOOK_URL Actions secret first.")
    if thread_mode == "text" and not bot_token:
        raise RuntimeError("Set DISCORD_BOT_TOKEN for text-channel threads, or select forum mode.")
    for release in pending:
        thread_id = deliver_release(release, source, webhook, thread_mode, bot_token, state)
        print(f"Announced {release['tag_name']} with bug-report thread {thread_id}.")
    return 0

if __name__ == "__main__":
    try:
        sys.exit(main())
    except (ValueError, RuntimeError, HTTPError, URLError, OSError, subprocess.CalledProcessError) as error:
        # Avoid leaking the URL from urllib/HTTP errors or subprocess arguments.
        if isinstance(error, (HTTPError, URLError)):
            print("Error contacting GitHub API or Discord.", file=sys.stderr)
        elif isinstance(error, subprocess.CalledProcessError):
            print("Git state commit/push failed; a retry may resend the release.", file=sys.stderr)
        else:
            print(f"Error: {error}", file=sys.stderr)
        sys.exit(1)
