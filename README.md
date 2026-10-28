# openOMSI Discord Release Center

A small, **webhook-only** release announcer for the official [openOMSI](https://github.com/openOMSI-org/openOMSI) releases. Python standard library + GitHub Actions; no Discord bot, paid service, server, database, or separate AI API needed.

## What it does

- Checks GitHub Releases on a best-effort ~5-minute schedule (stable releases **and** pre-releases; never drafts).
- Publishes **one** polished Discord embed per new release, using official notes in English, with sections for features, improvements, fixes and known issues where supported by the source text.
- Links **actual published release assets** by platform: Windows, macOS, Linux, Android and dedicated servers. A fallback link opens all downloads.
- Shows published date, release track (stable/beta), comparisons to the preceding release, and bug report destinations: [GitHub Issues](https://github.com/openOMSI-org/openOMSI/issues/new/choose) or <#1554233571764346940>.
- Stores release ID, Discord message ID, webhook ID and fingerprint in `state/announced.json`. When official notes or assets change, **edits the existing webhook message** instead of reposting it.
- Performs webhook credential checks **even when no new release exists**, and generates GitHub Actions summaries.
- On Actions failure, opens/updates one diagnostic issue in this repository; after a successful run, closes the issue.
- Offers `preview_latest`, `test_webhook` and `healthcheck` manual modes, plus offline tests.

> It does not publish game builds, translate source notes, automatically create GitHub bug issues, or post in a Discord channel different from the one associated with your webhook. Discord channel ID `1554233571764346940` is only a **link to the report channel**.

## Setup

1. Create a Discord webhook **in the channel where announcements should appear**. Copy its full HTTPS URL without adding `/github`.
2. Open [this repository's Actions Secrets](https://github.com/isaacsa2/openomsi-discord-releases/settings/secrets/actions), create `DISCORD_WEBHOOK_URL`, and paste the URL. **Never share it in chat, commits, issues or logs.**
3. In **Settings → Actions → General**, ensure Actions is enabled and the workflow token may write repository contents and issues. Organization/repository policy may override requested permissions.
4. Open **Actions → Announce openOMSI releases on Discord → Run workflow**:
   - `preview_latest`: prints JSON preview only; no Discord action or state change.
   - `healthcheck`: verifies GitHub releases and the webhook credential **without posting**.
   - `test_webhook`: posts a clearly marked test announcement; **does not** alter tracked state.
   - `check`: checks new releases and updates existing announcements.

The tracked baseline starts at **v0.3.45** (October 27, 2026). Versions already in `announced_release_ids` will **not** be announced retroactively, and legacy entries without message IDs cannot be edited.

## Configuration

In [`.github/workflows/announce.yml`](.github/workflows/announce.yml):

| Variable | Default | Purpose |
|---|---|---|
| `DISCORD_THREAD_MODE` | `webhook` | **One webhook message**, no thread or bot. Optional legacy `text` and `forum` thread modes remain available. |
| `DISCORD_REPORT_CHANNEL_ID` | `1554233571764346940` | Mention in the bug-report section, not the announcement destination. |
| `ANNOUNCE_PRERELEASES` | `true` | Include beta releases. |
| `EDIT_RELEASE_UPDATES` | `true` | Edit previously announced posts when official notes or assets change. |
| `SOURCE_REPOSITORY` | `openOMSI-org/openOMSI` | GitHub release source. |
| `WORKFLOW_MODE` | `check` | Automatic scheduled behavior. |

## Reliability / safety

- The webhook GET health check runs during normal checks, not just when a version is released. A missing/invalid secret causes the workflow to fail; GitHub issue alerts require `issues: write` permission.
- Discord's `wait=true` response supplies the message ID needed for edits. Edits use `PATCH` against the original webhook message; deleted messages fail rather than being recreated and spammed.
- API errors and rate limits are handled with bounded retry and safe errors. The bot only logs nonsecret identifiers.
- The release history is committed to the repo after each accepted action. If Discord accepts a **new** post but the network disconnects before its message ID is saved, or Git push fails after sending, **an exact-once guarantee is impossible** without an external transactional store: a duplicate may occur on retry. The code does not falsely claim stronger guarantees.
- If the webhook secret is replaced with a **different webhook ID**, existing messages cannot be edited with it; this is detected and reported. If you intentionally move the announcement destination, reset/migrate state carefully.
- The scheduler runs on GitHub's best-effort clock; it is **not realtime**. Public-repo schedules can be disabled after 60 days of inactivity, and an already disabled scheduler cannot alert itself. Check the Actions tab occasionally.
- The workflow preserves the pre-existing optional text/forum thread implementation. Default mode remains webhook-only even if no bot token exists.

## Development

```bash
python -m unittest discover -s tests -v
```

Project files:

- `scripts/announce_release.py` — releases API, fingerprints, publishing, edit sync, checkpoints and health status.
- `scripts/discord_delivery.py` — Discord payloads, download links, markdown formatting, HTTP send/edit and credential checks.
- `.github/workflows/announce.yml` — automated schedule and failure issue lifecycle.
- `state/announced.json` — tracked history; no tokens stored.
- `tests/` — offline regression tests, HTTP mocks and state-migration checks.

Licensed under MIT. This is a community automation, not an official openOMSI release publisher.
