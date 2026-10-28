"""Discord delivery and per-release bug-report threads (stdlib only)."""

from __future__ import annotations

import json
import re
import time
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from urllib.request import Request, urlopen

USER_AGENT = "openOMSI-Discord-Release-Announcer/2.0"
DISCORD_API = "https://discord.com/api/v10"
CHANGELOG_LIMIT = 2200
THREAD_MODES = ("text", "forum")


def clean_mentions(value: str) -> str:
    """Neutralize literal mass pings and keep them out of the webhook entirely."""
    value = re.sub(r"@(everyone|here)\b", lambda match: "@\u200b" + match.group(1), value, flags=re.I)
    return re.sub(r"<@([!&]?\d{1,25})>", lambda match: "<@\u200b" + match.group(1) + ">", value)


def format_changelog(body: str, release_url: str) -> str:
    """Present abridged *official* notes, never AI-generated or invented entries."""
    source = (body or "").replace("\r\n", "\n").strip()
    source = re.split(r"(?im)^\s{0,3}#{1,6}\s+Downloads?\b", source, maxsplit=1)[0].strip()
    if not source:
        return f"No release notes available yet. [Read the release]({release_url})."

    # Keep the main news, not enormous download tables or installation boilerplate.
    bullet_chunks = []
    current = None
    for line in source.splitlines():
        line = line.strip()
        if re.match(r"^[-*]\s+", line):
            if current:
                bullet_chunks.append(current)
            current = re.sub(r"^[-*]\s+", "", line)
        elif current is not None and line and not line.startswith(("#", "|", ">")):
            current += " " + line
    if current:
        bullet_chunks.append(current)
    # Fallback if release author doesn't use bullets.
    if not bullet_chunks:
        cleaned = clean_mentions(source)
        if len(cleaned) > CHANGELOG_LIMIT:
            cleaned = cleaned[: CHANGELOG_LIMIT - 100].rsplit(" ", 1)[0].rstrip() + "…"
        return cleaned + f"\n\n[Full release notes]({release_url})"

    selected = []
    used = 0
    for raw in bullet_chunks:
        # Prevent markdown lines, control chars and malicious mass mentions.
        item = clean_mentions(re.sub(r"[\x00-\x1f]", " ", raw)).strip()
        if len(item) > 410:
            item = item[:407].rsplit(" ", 1)[0].rstrip() + "…"
        line = "• " + item
        if selected and (used + len(line) > CHANGELOG_LIMIT or len(selected) >= 7):
            break
        selected.append(line)
        used += len(line) + 1
    return "\n".join(selected) + f"\n\n**[Read the full changelog]({release_url})**"


def report_thread_name(tag: str, test: bool = False) -> str:
    name = f"{'TEST • ' if test else ''}🐛 Bug Reports • openOMSI {tag}"
    return clean_mentions(name)[:100]


def report_template(tag: str, release_url: str, test: bool = False) -> dict:
    prefix = "**TEST THREAD — no real issues are needed here.**\n\n" if test else ""
    return {
        "username": "openOMSI Releases",
        "allowed_mentions": {"parse": []},
        "content": (
            f"{prefix}## 🐛 Bug Reports & Feedback — {clean_mentions(tag)}\n"
            "Found an issue in this version? Please report it **in this thread**.\n\n"
            "**Copy this template:**\n"
            "**Platform / OS:** Windows, Linux, macOS or Android + version\n"
            "**openOMSI version:** " + clean_mentions(tag) + "\n"
            "**Steps to reproduce:** 1. … 2. … 3. …\n"
            "**Expected behavior:** …\n"
            "**Actual behavior:** …\n"
            "**Logs / screenshots / video:** Attach files or paste a safe link\n\n"
            "Please search for existing reports, avoid personal information or tokens "
            "in logs, and include your hardware/graphics backend when relevant.\n"
            f"**Official release:** {release_url}\n"
            "This thread collects community reports; for formal tracking, open a GitHub issue: "
            "https://github.com/openOMSI-org/openOMSI/issues/new/choose"
        ),
    }


def make_payload(release: dict, source_repo: str, test: bool = False, forum: bool = False) -> dict:
    tag = clean_mentions(str(release["tag_name"]))
    release_url = str(release["html_url"])
    preview = bool(release.get("prerelease"))
    category = "BETA / PRE-RELEASE" if preview else "STABLE RELEASE"
    icon = "🧪" if preview else "🚀"
    title = f"{icon} openOMSI {tag}  •  {category}"
    if test:
        title = "🧪 TEST • " + title

    embed = {
        "author": {
            "name": "openOMSI  /  RELEASE CENTER",
            "url": f"https://github.com/{source_repo}",
            "icon_url": "https://github.com/openOMSI-org.png",
        },
        "title": title[:256],
        "url": release_url,
        "description": "### ✨ WHAT'S NEW\n" + format_changelog(str(release.get("body") or ""), release_url),
        "color": 0xF59E0B if preview else 0x2986F6,
        "fields": [
            {"name": "📦 VERSION", "value": f"`{tag[:100]}`", "inline": True},
            {"name": "🏷️ RELEASE TRACK", "value": "Preview / Beta" if preview else "Stable", "inline": True},
            {"name": "📥 DOWNLOAD", "value": f"[**Get this release →**]({release_url})", "inline": False},
            {"name": "🐛 FOUND A BUG?", "value": "Open the **Bug Reports** thread attached to this release and share your report.", "inline": False},
        ],
        "thumbnail": {"url": "https://github.com/openOMSI-org.png"},
        "footer": {"text": f"Official release notes • {source_repo} • Automated announcements"[:2048]},
        "timestamp": str(release["published_at"]),
    }
    payload = {"username": "openOMSI Releases", "allowed_mentions": {"parse": []}, "embeds": [embed]}
    if forum:
        payload["thread_name"] = report_thread_name(tag, test=test)
    return payload


def webhook_url_with_wait(webhook_url: str, thread_id: str = "") -> str:
    parsed = urlsplit(webhook_url)
    if (parsed.scheme != "https" or parsed.hostname not in
            {"discord.com", "discordapp.com", "ptb.discord.com", "canary.discord.com"}
            or not re.fullmatch(r"/api(?:/v\d+)?/webhooks/\d+/[^/]+/?", parsed.path)
            or parsed.username or parsed.password or parsed.port not in (None, 443)):
        raise ValueError("DISCORD_WEBHOOK_URL must be an HTTPS Discord channel webhook.")
    params = dict(parse_qsl(parsed.query, keep_blank_values=True))
    if "thread_id" in params:
        raise ValueError("Use the channel webhook, not a webhook URL with thread_id.")
    params["wait"] = "true"
    if thread_id:
        if not re.fullmatch(r"\d{15,25}", str(thread_id)):
            raise ValueError("Invalid Discord thread ID.")
        params["thread_id"] = str(thread_id)
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(params), ""))


def post_json(url: str, payload: dict, bot_token: str = "") -> dict:
    headers = {"Content-Type": "application/json", "User-Agent": USER_AGENT}
    if bot_token:
        headers["Authorization"] = "Bot " + bot_token
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    for attempt in range(4):
        request = Request(url, data=data, headers=headers, method="POST")
        try:
            with urlopen(request, timeout=25) as response:
                if response.status not in (200, 201):
                    raise RuntimeError(f"Unexpected Discord status: {response.status}")
                response_json = json.load(response)
                if not isinstance(response_json, dict):
                    raise RuntimeError("Discord response was not an object.")
                return response_json
        except HTTPError as err:
            # Never log the URL or error body: either can contain the webhook token.
            if err.code == 429 and attempt < 3:
                try:
                    delay = min(max(float(err.headers.get("Retry-After", "2")), 1.0), 30.0)
                except (TypeError, ValueError):
                    delay = 2.0
                time.sleep(delay)
                continue
            if err.code in (500, 502, 503, 504) and attempt < 3:
                time.sleep(min(2 ** attempt, 8))
                continue
            raise RuntimeError(f"Discord request failed (HTTP {err.code}).") from None
        except URLError:
            if attempt < 3:
                time.sleep(min(2 ** attempt, 8))
                continue
            raise RuntimeError("Network error communicating with Discord.") from None
    raise RuntimeError("Discord attempt limit exceeded.")


def send_discord(payload: dict, webhook_url: str, thread_id: str = "") -> dict:
    return post_json(webhook_url_with_wait(webhook_url, thread_id), payload)


def start_report_thread(channel_id: str, message_id: str, name: str, bot_token: str) -> str:
    """Only for regular text/announcement channels. Id is deterministic: message id."""
    if not bot_token:
        raise RuntimeError("DISCORD_BOT_TOKEN is required in text thread mode.")
    for value in (channel_id, message_id):
        if not re.fullmatch(r"\d{15,25}", str(value)):
            raise RuntimeError("Discord did not return a valid channel/message ID.")
    url = f"{DISCORD_API}/channels/{channel_id}/messages/{message_id}/threads"
    try:
        result = post_json(url, {"name": name[:100], "auto_archive_duration": 1440}, bot_token)
        thread_id = str(result.get("id", ""))
        if not re.fullmatch(r"\d{15,25}", thread_id):
            raise RuntimeError("Discord did not return a valid thread ID.")
        return thread_id
    except RuntimeError as err:
        # Text-channel thread ID equals its starter message ID. A previous run may
        # have created the thread before the state push failed; verify on retry.
        if "HTTP 400" not in str(err):
            raise
        request = Request(f"{DISCORD_API}/channels/{message_id}", headers={
            "Authorization": "Bot " + bot_token, "User-Agent": USER_AGENT})
        try:
            with urlopen(request, timeout=25) as response:
                thread = json.load(response)
            if str(thread.get("parent_id")) == str(channel_id) and str(thread.get("id")) == str(message_id):
                return str(message_id)
        except (HTTPError, URLError, ValueError, KeyError):
            pass
        raise
