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
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from urllib.request import Request, urlopen

STATE_PATH = Path(__file__).resolve().parent.parent / "state" / "announced.json"
API_BASE = "https://api.github.com"
USER_AGENT = "openOMSI-Discord-Release-Announcer/1.0"
MAX_PAGES = 5
MAX_RELEASES_PER_RUN = 5
DESCRIPTION_LIMIT = 3700  # Discord's limit is 4096; leave margin.


def parse_date(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def read_state(path: Path = STATE_PATH) -> dict:
    state = json.loads(path.read_text(encoding="utf-8"))
    parse_date(state["baseline_published_at"])
    state["announced_release_ids"] = list(map(int, state["announced_release_ids"]))
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


def format_changelog(body: str, release_url: str) -> str:
    body = (body or "").replace("\r\n", "\n").strip()
    # The release page contains the complete downloads table; keep the Discord embed readable.
    body = re.split(r"(?im)^\s{0,3}#{1,6}\s+Downloads?\b", body, maxsplit=1)[0].strip()
    if not body:
        body = "No release notes have been published yet. Check the official release page for details."
    # Prevent accidental channel-wide mentions even in a copied changelog.
    body = re.sub(r"@(everyone|here)\b", lambda m: "@\u200b" + m.group(1), body, flags=re.I)
    if len(body) > DESCRIPTION_LIMIT:
        cutoff = body.rfind("\n", 0, DESCRIPTION_LIMIT - 100)
        if cutoff < DESCRIPTION_LIMIT // 2:
            cutoff = DESCRIPTION_LIMIT - 100
        body = body[:cutoff].rstrip() + f"\n\n[Read the full changelog]({release_url})"
    return body


def make_payload(release: dict, source_repo: str, test: bool = False) -> dict:
    tag = str(release["tag_name"])
    url = str(release["html_url"])
    preview = bool(release.get("prerelease"))
    status = "Pre-release" if preview else "Stable release"
    title = f"openOMSI {tag} — {status}"
    if test:
        title = "TEST MESSAGE — " + title
    title = title[:256]
    embed = {
        "title": title,
        "url": url,
        "description": format_changelog(str(release.get("body") or ""), url),
        "color": 0xF59E0B if preview else 0x3B82F6,
        "fields": [
            {"name": "Version", "value": f"`{tag[:100]}`", "inline": True},
            {"name": "Channel", "value": "Preview" if preview else "Stable", "inline": True},
            {"name": "Download", "value": f"[View official release and downloads]({url})", "inline": False},
        ],
        "footer": {"text": f"Official release notes • {source_repo}"[:2048]},
        "timestamp": release["published_at"],
    }
    return {
        "username": "openOMSI Releases",
        "allowed_mentions": {"parse": []},
        "embeds": [embed],
    }


def webhook_url_with_wait(webhook_url: str) -> str:
    split = urlsplit(webhook_url)
    if (split.scheme != "https" or split.hostname not in
            {"discord.com", "discordapp.com", "ptb.discord.com", "canary.discord.com"} or
            not split.path.startswith("/api/webhooks/")):
        raise ValueError("DISCORD_WEBHOOK_URL must be a Discord HTTPS webhook URL.")
    params = dict(parse_qsl(split.query, keep_blank_values=True))
    params["wait"] = "true"
    return urlunsplit((split.scheme, split.netloc, split.path, urlencode(params), ""))


def send_discord(payload: dict, webhook_url: str):
    url = webhook_url_with_wait(webhook_url)
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    for attempt in range(4):
        request = Request(url, data=data, headers={"Content-Type": "application/json", "User-Agent": USER_AGENT}, method="POST")
        try:
            with urlopen(request, timeout=25) as response:
                if response.status not in (200, 204):
                    raise RuntimeError(f"Unexpected Discord status: {response.status}")
                return
        except HTTPError as err:
            # Don't include the exception message; it may reveal webhook URL/token.
            if err.code == 429 and attempt < 3:
                retry_after = err.headers.get("Retry-After", "2")
                try:
                    delay = min(max(float(retry_after), 1.0), 30.0)
                except ValueError:
                    delay = 2.0
                print(f"Discord rate limit; retrying in {delay:g} s.")
                time.sleep(delay)
                continue
            if err.code in (500, 502, 503, 504) and attempt < 3:
                time.sleep(min(2 ** attempt, 8))
                continue
            raise RuntimeError(f"Discord rejected webhook (HTTP {err.code}).") from None
        except URLError:
            if attempt < 3:
                time.sleep(min(2 ** attempt, 8))
                continue
            raise RuntimeError("Network error communicating with Discord.") from None
    raise RuntimeError("Webhook attempt limit exceeded.")


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
    source = os.getenv("SOURCE_REPOSITORY", "openOMSI-org/openOMSI")
    include_pre = os.getenv("ANNOUNCE_PRERELEASES", "true").lower() == "true"
    releases = fetch_releases(source, token)

    if mode in ("preview_latest", "test_webhook"):
        published = [r for r in releases if r.get("published_at") and not r.get("draft")]
        if not published:
            print("No published releases found.")
            return 0
        latest = max(published, key=lambda r: (parse_date(r["published_at"]), int(r["id"])))
        payload = make_payload(latest, source, test=(mode == "test_webhook"))
        if mode == "preview_latest":
            print(json.dumps(payload, indent=2, ensure_ascii=False))
            return 0
        if not webhook:
            raise RuntimeError("Set the DISCORD_WEBHOOK_URL Actions secret before sending a test.")
        send_discord(payload, webhook)
        print(f"Test message sent for {latest['tag_name']} (state unchanged).")
        return 0

    state = read_state()
    pending = eligible_releases(releases, state, include_pre)
    if not pending:
        print("No new releases to announce.")
        return 0
    if not webhook:
        raise RuntimeError("Set the DISCORD_WEBHOOK_URL Actions secret first.")
    for release in pending:
        payload = make_payload(release, source)
        send_discord(payload, webhook)
        state["announced_release_ids"].append(int(release["id"]))
        # Save and push after every post, not only after the whole batch.
        write_and_push_state(state, release)
        print(f"Announced {release['tag_name']}.")
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
