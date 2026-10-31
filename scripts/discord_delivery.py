"""Render GitHub release notes and deliver Discord webhooks (stdlib only).

No bot token is necessary in the default webhook mode. Secrets must never enter
logs, repository files, or exception messages.
"""
from __future__ import annotations

import json
import re
import time
from datetime import datetime
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit
from urllib.request import Request, urlopen

USER_AGENT = "openOMSI-Discord-Release-Announcer/3.0"
DISCORD_API = "https://discord.com/api/v10"
THREAD_MODES = ("webhook", "text", "forum")
DEFAULT_REPORT_CHANNEL_ID = "1554233571764346940"
CHANGELOG_LIMIT = 2400
GITHUB_REPO_RE = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z")
SNOWFLAKE_RE = re.compile(r"\d{15,25}\Z")


def clean_mentions(value: str) -> str:
    value = re.sub(r"@(everyone|here)\b", lambda m: "@\u200b" + m.group(1), value, flags=re.I)
    return re.sub(r"<@([!&]?\d{1,25})>", lambda m: "<@\u200b" + m.group(1) + ">", value)


def canonical_heading(header: str) -> str:
    h = header.strip().lower()
    if any(word in h for word in ("known issue", "limitation", "regression")):
        return "⚠️ Known Issues"
    if any(word in h for word in ("fix", "bug", "crash", "stabilit")):
        return "🛠️ Bug Fixes"
    if any(word in h for word in ("feature", "new", "added", "addition")):
        return "✨ New Features"
    if any(word in h for word in ("improv", "performance", "optimi", "change", "control")):
        return "⚡ Improvements"
    return "📋 Other Changes"


def classify_bullet(bullet: str) -> str:
    """Use generic grouping only when upstream doesn't provide useful headings."""
    value = bullet.lower()
    if re.search(r"\b(known issue|workaround|not yet|does not work)\b", value):
        return "⚠️ Known Issues"
    if re.search(r"\b(fix(?:ed|es)?|crash(?:es|ed)?|regression|bug(?:s)?|resolve[ds]?)\b", value):
        return "🛠️ Bug Fixes"
    if re.search(r"\b(new|add(?:ed|s)?|introduc(?:e|ed))\b", value):
        return "✨ New Features"
    return "⚡ Improvements"


def format_changelog(body: str, release_url: str) -> str:
    """Preserve the author's words, organize them, and fit in Discord's limits."""
    source = (body or "").replace("\r\n", "\n").strip()
    source = re.split(r"(?im)^\s{0,3}#{1,6}\s+Downloads?\b", source, maxsplit=1)[0].strip()
    link = f"\n\n[Full release notes]({release_url})"
    if not source:
        return "No release notes published yet." + link

    categories: dict[str, list[str]] = {}
    heading = ""
    current = ""
    bullets: list[tuple[str, str]] = []

    def save_bullet():
        nonlocal current
        if current:
            bullets.append((heading, current))
            current = ""

    for line in source.splitlines():
        line = line.strip()
        if re.match(r"^#{1,6}\s+", line):
            save_bullet()
            heading = re.sub(r"^#{1,6}\s+", "", line)
        elif re.match(r"^[-*]\s+", line):
            save_bullet()
            current = re.sub(r"^[-*]\s+", "", line)
        elif current and line and not line.startswith(("|", ">")):
            current += " " + line
    save_bullet()

    if not bullets:
        clean = clean_mentions(re.sub(r"[\x00-\x1f]", " ", source))
        reserve = CHANGELOG_LIMIT - len(link) - 8
        return (clean[:reserve].rsplit(" ", 1)[0] + "…" if len(clean) > reserve else clean) + link

    for source_heading, bullet in bullets:
        bucket = canonical_heading(source_heading) if source_heading else classify_bullet(bullet)
        bullet = clean_mentions(re.sub(r"[\x00-\x1f]", " ", bullet)).strip()
        if len(bullet) > 335:
            bullet = bullet[:332].rsplit(" ", 1)[0].rstrip() + "…"
        categories.setdefault(bucket, []).append("• " + bullet)

    priority = ("✨ New Features", "⚡ Improvements", "🛠️ Bug Fixes", "⚠️ Known Issues", "📋 Other Changes")
    # Fairly sample separate sections: a long feature list must not hide fixes.
    chosen: dict[str, list[str]] = {k: [] for k in priority}
    budget = CHANGELOG_LIMIT - len(link) - 3
    consumed = 0
    selected = 0
    for index in range(7):
        # Two rounds per category first, then fill remaining slots in priority order.
        sequence = [k for k in priority if len(chosen[k]) < min(2, len(categories.get(k, [])))]
        if not sequence:
            sequence = [k for k in priority if len(chosen[k]) < len(categories.get(k, []))]
        if not sequence:
            break
        added = False
        for category in sequence:
            entry = categories[category][len(chosen[category])]
            overhead = len(category) + 8 if not chosen[category] else 0
            if consumed + len(entry) + overhead > budget:
                continue
            chosen[category].append(entry)
            consumed += len(entry) + overhead + 1
            selected += 1
            added = True
            break
        if not added:
            break
    if not selected:
        return "Read the official release notes for the full list of changes." + link
    lines = []
    for category in priority:
        if chosen[category]:
            lines.append("**" + category + "**\n" + "\n".join(chosen[category]))
    return "\n\n".join(lines).strip() + link


def download_links(release: dict) -> list[dict]:
    """Build compact, phone-friendly download fields from real release assets."""
    assets = release.get("assets") or []
    by_platform: dict[str, list[str]] = {}
    for asset in assets:
        name = str(asset.get("name", ""))
        url = str(asset.get("browser_download_url", ""))
        if not (name and url.startswith("https://github.com/") and "/releases/download/" in url):
            continue
        low = name.lower()
        if low in ("sha256sums", "checksums.txt") or low.endswith((".sig", ".sha256")):
            continue
        if "server" in low:
            platform = "🖧 Dedicated Server"
        elif any(x in low for x in ("windows", "win64", "win32")):
            platform = "🪟 Windows"
        elif any(x in low for x in ("android", ".apk")):
            platform = "📱 Android"
        elif any(x in low for x in ("macos", "darwin", ".dmg")):
            platform = "🍎 macOS"
        elif "linux" in low:
            platform = "🐧 Linux"
        else:
            continue
        ext = "." + name.rsplit(".", 1)[-1].lower() if "." in name else "file"
        arch = "ARM64" if any(x in low for x in ("arm64", "aarch64")) else ("x64" if any(x in low for x in ("x64", "amd64")) else "")
        suffix = "Installer" if "setup" in low or ext == ".msi" else ext.lstrip(".").upper()
        label = " · ".join(filter(None, (arch, suffix)))
        by_platform.setdefault(platform, []).append(f"[{label[:50]}]({url})")

    fields = []
    for platform in ("🪟 Windows", "🐧 Linux", "🍎 macOS", "📱 Android", "🖧 Dedicated Server"):
        entries = list(dict.fromkeys(by_platform.get(platform, [])))
        if not entries:
            continue
        # Compact two-column fields on desktop, stacked fields on phones.
        # Reserve room for the rest of the embed even with long asset URLs.
        chosen = []
        for entry in entries[:4]:
            next_value = "\n".join(f"• {link}" for link in chosen + [entry])
            if len(next_value) <= 400:
                chosen.append(entry)
        if chosen:
            fields.append({"name": platform, "value": "\n".join(f"• {link}" for link in chosen), "inline": True})
    return fields


def release_highlight(body: str, limit: int = 320) -> str:
    """Reuse an official 'In short' paragraph without inventing a summary."""
    source = (body or "").replace("\r\n", "\n")
    match = re.search(r"(?im)^\s{0,3}#{2,4}\s+(?:in short|summary|overview)\b[^\n]*\n", source)
    if not match:
        return ""
    lines = []
    for raw in source[match.end():].splitlines():
        line = raw.strip()
        if not line and not lines:
            continue
        if not line or line.startswith(("#", "-", "*", "|", ">")):
            break
        lines.append(line)
    summary = clean_mentions(re.sub(r"[\x00-\x1f]", " ", " ".join(lines))).strip()
    if len(summary) > limit:
        summary = summary[:limit - 1].rsplit(" ", 1)[0].rstrip() + "…"
    return summary


def report_thread_name(tag: str, test: bool = False) -> str:
    return clean_mentions(f"{'TEST • ' if test else ''}🐛 Bug Reports • openOMSI {tag}")[:100]


def report_template(tag: str, release_url: str, test: bool = False) -> dict:
    prefix = "**TEST THREAD — no real issues are needed here.**\n\n" if test else ""
    return {"username": "openOMSI Releases", "allowed_mentions": {"parse": []}, "content": (
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
        "For formal tracking, open a GitHub issue: "
        "https://github.com/openOMSI-org/openOMSI/issues/new/choose")}


def make_payload(release: dict, source_repo: str, test: bool = False, forum: bool = False,
                 thread_mode: str = "webhook", report_channel_id: str = DEFAULT_REPORT_CHANNEL_ID,
                 previous_tag: str = "") -> dict:
    if thread_mode not in THREAD_MODES or not GITHUB_REPO_RE.fullmatch(source_repo):
        raise ValueError("Invalid release source or Discord thread mode.")
    tag = clean_mentions(str(release["tag_name"]))
    release_url = str(release["html_url"])
    preview = bool(release.get("prerelease"))
    critical = bool(re.search(r"(?i)\b(critical security|critical issue|security advisory|breaking change)\b",
                              str(release.get("body") or "")))
    track = "BETA / PRE-RELEASE" if preview else "STABLE RELEASE"
    icon = "🧪" if preview else "🟢"
    title = f"{icon} openOMSI {tag}  •  {track}"
    if test:
        title = "🧪 TEST • " + title

    if thread_mode == "webhook" and not forum:
        if not SNOWFLAKE_RE.fullmatch(report_channel_id):
            raise ValueError("DISCORD_REPORT_CHANNEL_ID must be a numeric Discord channel ID.")
        bug_help = (f"[Report on GitHub](https://github.com/{source_repo}/issues/new/choose)"
                    f"  •  <#{report_channel_id}>\n"
                    "Include your platform, steps to reproduce and logs.")
    else:
        bug_help = "Open the **Bug Reports** thread attached to this release."

    try:
        date_display = f"<t:{int(datetime.fromisoformat(str(release['published_at']).replace('Z', '+00:00')).timestamp())}:D>"
    except (ValueError, OverflowError):
        date_display = str(release["published_at"])[:10]

    fields = [
        {"name": "📦 VERSION", "value": f"{chr(96)}{tag[:100]}{chr(96)}", "inline": True},
        {"name": "🏷️ RELEASE TRACK", "value": "🧪 Beta / preview" if preview else "🟢 Stable", "inline": True},
        {"name": "📅 RELEASED", "value": date_display, "inline": True},
    ]
    fields.extend(download_links(release))
    fields.append({"name": "📥 ALL DOWNLOADS", "value": f"[Browse official release files]({release_url})", "inline": False})
    if previous_tag and re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", previous_tag) and re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", str(release["tag_name"])):
        compare_url = f"https://github.com/{source_repo}/compare/{quote(previous_tag)}...{quote(str(release['tag_name']))}"
        fields.append({"name": "🔍 COMPARE", "value": f"[Changes since {previous_tag}]({compare_url})", "inline": False})
    fields.append({"name": "🐛 FOUND A BUG?", "value": bug_help, "inline": False})

    status = ("**🧪 BETA BUILD**  ·  A preview for testing; bugs and regressions are possible."
              if preview else "**🟢 STABLE RELEASE**  ·  Available through the official GitHub release.")
    if test:
        status = "**TEST ANNOUNCEMENT — NOT A REAL RELEASE**\n" + status
    highlight = release_highlight(str(release.get("body") or ""))
    intro = status + (f"\n\n**📰 IN SHORT**\n{highlight}" if highlight else "")
    description = intro + "\n\n**✨ WHAT'S NEW**\n" + format_changelog(str(release.get("body") or ""), release_url)

    embed = {
        "author": {"name": "openOMSI  /  RELEASE NOTES", "url": f"https://github.com/{source_repo}",
                   "icon_url": "https://github.com/openOMSI-org.png"},
        "title": title[:256], "url": release_url,
        "description": description[:4096],
        "color": 0xD73A49 if critical else (0xE8A317 if preview else 0x238636),
        "fields": fields,
        "thumbnail": {"url": "https://github.com/openOMSI-org.png"},
        "footer": {"text": "openOMSI • GitHub Releases • Automated update"},
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
        if not SNOWFLAKE_RE.fullmatch(str(thread_id)):
            raise ValueError("Invalid Discord thread ID.")
        params["thread_id"] = str(thread_id)
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(params), ""))


def request_json(url: str, payload: dict | None = None, bot_token: str = "", method: str = "POST") -> dict:
    headers = {"Content-Type": "application/json", "User-Agent": USER_AGENT}
    if bot_token:
        headers["Authorization"] = "Bot " + bot_token
    data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
    for attempt in range(4):
        request = Request(url, data=data, headers=headers, method=method)
        try:
            with urlopen(request, timeout=25) as response:
                if response.status not in (200, 201):
                    raise RuntimeError(f"Unexpected Discord HTTP status: {response.status}")
                obj = json.load(response)
                if not isinstance(obj, dict):
                    raise RuntimeError("Discord response was not an object.")
                return obj
        except HTTPError as err:
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
            # Never log URL, webhook token, headers, or body.
            raise RuntimeError(f"Discord request failed (HTTP {err.code}).") from None
        except URLError:
            if attempt < 3:
                time.sleep(min(2 ** attempt, 8))
                continue
            raise RuntimeError("Network error communicating with Discord.") from None
    raise RuntimeError("Discord attempt limit exceeded.")


def send_discord(payload: dict, webhook_url: str, thread_id: str = "") -> dict:
    return request_json(webhook_url_with_wait(webhook_url, thread_id), payload)


def edit_discord(payload: dict, webhook_url: str, message_id: str) -> dict:
    if not SNOWFLAKE_RE.fullmatch(str(message_id)):
        raise ValueError("Cannot edit without a valid Discord message ID.")
    # Webhook edits take a limited payload (username is ignored, thread_name is not supported).
    edit_payload = {"embeds": payload.get("embeds", []), "allowed_mentions": {"parse": []}}
    url = webhook_url_with_wait(webhook_url)
    parsed = urlsplit(url)
    url = urlunsplit((parsed.scheme, parsed.netloc, parsed.path.rstrip("/") + f"/messages/{message_id}", "", ""))
    return request_json(url, edit_payload, method="PATCH")


def check_discord_webhook(webhook_url: str) -> str:
    """Check secret validity even when no new release is available (no message sent)."""
    url = webhook_url_with_wait(webhook_url)
    parsed = urlsplit(url)
    clean_url = urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))
    result = request_json(clean_url, method="GET")
    if not SNOWFLAKE_RE.fullmatch(str(result.get("id", ""))):
        raise RuntimeError("Discord webhook lookup returned no valid ID.")
    return str(result["id"])


def start_report_thread(channel_id: str, message_id: str, name: str, bot_token: str) -> str:
    """Legacy optional thread mode, requiring a Discord bot."""
    if not bot_token:
        raise RuntimeError("DISCORD_BOT_TOKEN is required in text thread mode.")
    for value in (channel_id, message_id):
        if not SNOWFLAKE_RE.fullmatch(str(value)):
            raise RuntimeError("Discord did not return a valid channel/message ID.")
    url = f"{DISCORD_API}/channels/{channel_id}/messages/{message_id}/threads"
    try:
        result = request_json(url, {"name": name[:100], "auto_archive_duration": 1440}, bot_token)
        thread_id = str(result.get("id", ""))
        if not SNOWFLAKE_RE.fullmatch(thread_id):
            raise RuntimeError("Discord did not return a valid thread ID.")
        return thread_id
    except RuntimeError as err:
        if "HTTP 400" not in str(err):
            raise
        try:
            thread = request_json(f"{DISCORD_API}/channels/{message_id}", bot_token=bot_token, method="GET")
            if str(thread.get("parent_id")) == str(channel_id) and str(thread.get("id")) == str(message_id):
                return str(message_id)
        except RuntimeError:
            pass
        raise
