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
    if any(word in h for word in ("known", "limitation", "regression", "follow-up", "caveat")):
        return "⚠️ Known Issues"
    if any(word in h for word in ("security", "hardening", "project", "check", "infrastructure")):
        return "🔒 Security & Project"
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


def release_intro(source: str) -> str:
    """The leading blockquote (e.g. "Beta, for testing...") becomes a short callout."""
    lead = re.split(r"(?m)^\s{0,3}(?:#{1,6}\s|[-*]\s)", source, maxsplit=1)[0]
    quoted = [line.strip()[1:].strip() for line in lead.splitlines() if line.strip().startswith(">")]
    text = clean_mentions(re.sub(r"[\x00-\x1f]", " ", " ".join(quoted))).strip()
    if not text:
        return ""
    if len(text) > 300:
        text = text[:297].rsplit(" ", 1)[0].rstrip() + "…"
    return "> " + text + "\n\n"


def format_changelog(body: str, release_url: str) -> str:
    """Preserve the author's words, organize them, and fit in Discord's limits."""
    source = (body or "").replace("\r\n", "\n").strip()
    source = re.split(r"(?im)^\s{0,3}#{1,6}\s+Downloads?\b", source, maxsplit=1)[0].strip()
    link = f"\n\n[Full release notes]({release_url})"
    if not source:
        return "No release notes published yet." + link
    # PR links cost ~60 chars each; keep the number so more bullets fit.
    pr_link = r"\[(#\d+)\]\(https://github\.com/[^)\s]+\)"
    source = re.sub(r"\(" + pr_link + r"\)", r"(\1)", source)
    source = re.sub(pr_link, r"\1", source)
    intro = release_intro(source)

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

    priority = ("✨ New Features", "⚡ Improvements", "🛠️ Bug Fixes", "🔒 Security & Project",
                "⚠️ Known Issues", "📋 Other Changes")
    # Fairly sample separate sections: a long feature list must not hide fixes.
    chosen: dict[str, list[str]] = {k: [] for k in priority}
    budget = CHANGELOG_LIMIT - len(link) - len(intro) - 3
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
        return intro + "Read the official release notes for the full list of changes." + link
    lines = []
    for category in priority:
        if chosen[category]:
            lines.append("**" + category + "**\n" + "\n".join(chosen[category]))
    return intro + "\n\n".join(lines).strip() + link


def download_links(release: dict, budget: int = 2200) -> list[dict]:
    """Only use actual official release assets; never invent platform support.

    ``budget`` caps the characters of all platform fields together so the whole
    embed stays below Discord's 6000-char limit; the "all downloads" link covers
    anything left out.
    """
    assets = release.get("assets") or []
    by_platform: dict[str, list[tuple[tuple[str, int], str]]] = {}
    for asset in assets:
        name = str(asset.get("name", ""))
        url = str(asset.get("browser_download_url", ""))
        if not (name and url.startswith("https://github.com/") and "/releases/download/" in url):
            continue
        low = name.lower()
        if low in ("sha256sums", "checksums.txt") or low.endswith((".sig", ".sha256")):
            continue
        os_name = ""
        if "server" in low:
            platform = "🖧 Dedicated Server"
            os_name = ("Windows" if any(x in low for x in ("windows", "win64", "win32")) else
                       "Linux" if "linux" in low else "macOS" if any(x in low for x in ("macos", "darwin")) else "")
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
        if platform == "🍎 macOS" and arch:
            arch = "Apple Silicon" if arch == "ARM64" else "Intel"
        suffix = "Installer" if "setup" in low or ext == ".msi" else ext.lstrip(".").upper()
        label = " ".join(filter(None, (os_name, arch, suffix)))
        order = (os_name, {"x64": 0, "Intel": 0, "ARM64": 1, "Apple Silicon": 1}.get(arch, 2))
        by_platform.setdefault(platform, []).append((order, f"[{label[:50]}]({url})"))

    platforms = [p for p in ("🪟 Windows", "🐧 Linux", "🍎 macOS", "📱 Android", "🖧 Dedicated Server")
                 if p in by_platform]
    queues = {p: list(dict.fromkeys(e for _, e in sorted(by_platform[p], key=lambda x: x[0])))[:6]
              for p in platforms}
    chosen: dict[str, list[str]] = {p: [] for p in platforms}
    used = 0
    # Round-robin: every platform gets its first link before any gets a second.
    for _ in range(6):
        for platform in platforms:
            queue = queues[platform]
            if len(chosen[platform]) >= len(queue):
                continue
            entry = queue[len(chosen[platform])]
            cost = len(entry) + (len(platform) if not chosen[platform] else len(" • "))
            if used + cost > budget or len(" • ".join(chosen[platform] + [entry])) > 1024:
                continue
            chosen[platform].append(entry)
            used += cost
    return [{"name": p, "value": " • ".join(chosen[p]), "inline": True} for p in platforms if chosen[p]]


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


def discord_date(published_at: str) -> str:
    """Discord renders <t:...> in each reader's own timezone and language."""
    try:
        unix = int(datetime.fromisoformat(published_at.replace("Z", "+00:00")).timestamp())
    except ValueError:
        return published_at[:10]
    return f"<t:{unix}:D>\n<t:{unix}:R>"


def make_payload(release: dict, source_repo: str, test: bool = False, forum: bool = False,
                 thread_mode: str = "webhook", report_channel_id: str = DEFAULT_REPORT_CHANNEL_ID,
                 previous_tag: str = "") -> dict:
    if thread_mode not in THREAD_MODES or not GITHUB_REPO_RE.fullmatch(source_repo):
        raise ValueError("Invalid release source or Discord thread mode.")
    tag = clean_mentions(str(release["tag_name"]))
    release_url = str(release["html_url"])
    preview = bool(release.get("prerelease"))
    category = "BETA / PRE-RELEASE" if preview else "STABLE RELEASE"
    icon = "🧪" if preview else "🚀"
    critical = bool(re.search(r"(?i)\b(critical security|critical issue|security advisory|breaking change)\b", str(release.get("body") or "")))
    title = f"{icon} openOMSI {tag}  •  {category}"
    if test:
        title = "🧪 TEST • " + title
    if thread_mode == "webhook" and not forum:
        if not SNOWFLAKE_RE.fullmatch(report_channel_id):
            raise ValueError("DISCORD_REPORT_CHANNEL_ID must be a numeric Discord channel ID.")
        bug_help = (f"Report issues via [GitHub Issues](https://github.com/{source_repo}/issues/new/choose) "
                    f"or in <#{report_channel_id}>. Include platform, reproduction steps and logs.")
    else:
        bug_help = "Open the **Bug Reports** thread attached to this release."
    fields = [
        {"name": "📦 VERSION", "value": f"`{tag[:100]}`", "inline": True},
        {"name": "🏷️ RELEASE TRACK", "value": "Preview / Beta" if preview else "Stable", "inline": True},
        {"name": "📅 RELEASED", "value": discord_date(str(release["published_at"])), "inline": True},
    ]
    tail = [{"name": "📥 ALL DOWNLOADS", "value": f"[**View release assets →**]({release_url})", "inline": False}]
    if previous_tag and re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", previous_tag) and re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", str(release["tag_name"])):
        compare_url = f"https://github.com/{source_repo}/compare/{quote(previous_tag)}...{quote(str(release['tag_name']))}"
        tail.append({"name": "🔍 COMPARE", "value": f"[**Changes since {previous_tag}**]({compare_url})", "inline": False})
    tail.append({"name": "🐛 FOUND A BUG?", "value": bug_help, "inline": False})
    embed = {
        "author": {"name": "openOMSI • Release Center", "url": f"https://github.com/{source_repo}",
                   "icon_url": "https://github.com/openOMSI-org.png"},
        "title": title[:256], "url": release_url,
        "description": "### ✨ WHAT'S NEW\n" + format_changelog(str(release.get("body") or ""), release_url),
        "color": 0xDC2626 if critical else (0xF59E0B if preview else 0x2986F6),
        "fields": [],
        "thumbnail": {"url": "https://github.com/openOMSI-org.png"},
        "footer": {"text": f"Official release • {source_repo}"[:2048],
                   "icon_url": "https://github.com/openOMSI-org.png"},
        "timestamp": str(release["published_at"]),
    }
    # Download links get whatever is left of Discord's 6000-char embed limit (with margin).
    used = (len(embed["title"]) + len(embed["description"]) + len(embed["author"]["name"]) +
            len(embed["footer"]["text"]) + sum(len(f["name"]) + len(f["value"]) for f in fields + tail))
    embed["fields"] = fields + download_links(release, budget=max(0, 5800 - used)) + tail
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
