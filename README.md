# openOMSI Discord Release Announcer

A lightweight, automated Discord release announcement feed for the **official** [`openOMSI-org/openOMSI`](https://github.com/openOMSI-org/openOMSI) GitHub releases.

- Checks for new GitHub releases every ~5 minutes with GitHub Actions (schedules are best-effort, not instant).
- Publishes **stable releases and pre-releases**, but never drafts.
- Copies the **official release changelog** (normally authored in English), without inventing entries or using an AI API.
- Sends a polished Discord embed with a compact excerpt of the official changelog and official download links.
- **Default: webhook only**, with no bot or thread.
- Directs bug reports to [GitHub Issues](https://github.com/openOMSI-org/openOMSI/issues/new/choose) or Discord `<#1554233571764346940>`.
- Optional per-release threads can be enabled later in text or forum channels.
- Avoids repeat notifications using a tracked `state/announced.json` file.
- Seeds the last announced release to **v0.3.45** (published October 27, 2026), so old releases will not be spammed.
- Includes a manual test and preview mode.
- Uses only Python's standard library: no pip dependencies, dedicated server, or extra paid service.

> This is an **independent announcement bot**, not the official openOMSI release pipeline. It **does not build, tag, or publish** versions, and it **does not translate** non-English source notes. Every heading written by the bot is English; changelog text comes from the upstream release. Long changelogs are truncated in Discord with a link to the full notes.

## 1. Publish the repository under your account

On Windows, extract the ZIP, open PowerShell **inside this project folder** and run:

```powershell
# Install Git for Windows and GitHub CLI first; make sure you are signed in.
PowerShell -ExecutionPolicy Bypass -File .\setup.ps1
```

The script creates `isaacsa2/openomsi-discord-releases` as a **public GitHub repository** and pushes the files. It requires installed [`git`](https://git-scm.com/downloads/win) and [`gh`](https://cli.github.com/), and GitHub access to the `isaacsa2` account. Public repositories are intentional here for free Actions usage on public repositories; you can adjust visibility before publishing.

Alternatively, [create a new repository](https://github.com/new) under `isaacsa2` named `openomsi-discord-releases` and push this folder yourself.

## 2. Set your Discord webhook secret

1. In Discord, open your server channel's **Edit Channel → Integrations → Webhooks → New Webhook** and copy the URL.
2. In your GitHub repo, open **Settings → Secrets and variables → Actions → New repository secret**.
3. Enter name **`DISCORD_WEBHOOK_URL`**, paste the webhook URL as the value, and save.
4. **Do not paste your webhook URL in source files, issues, commits, or chat.** Do **not** append `/github`: this script sends Discord JSON directly.

## 2a. Default: webhook only (no bot required)

`DISCORD_THREAD_MODE: webhook` is now the default in `.github/workflows/announce.yml`.
It posts **one rich release announcement** in the Discord channel associated with your webhook.
It does **not** create a thread, send a second message, or require `DISCORD_BOT_TOKEN`.

The release embed includes **🐛 FOUND A BUG?** with:

- [GitHub Issues](https://github.com/openOMSI-org/openOMSI/issues/new/choose) for tracked bug reports.
- Discord channel `<#1554233571764346940>` to discuss problems with the community.

`DISCORD_REPORT_CHANNEL_ID` controls the Discord channel mention (default `1554233571764346940`). It does **not** change the channel to which the webhook posts. The announcement is still sent wherever the webhook was created.

### Optional auto-threads later

- Set `DISCORD_THREAD_MODE: text` to create a thread attached to the announcement in a text channel. Invite a Discord bot with **View Channel**, **Read Message History**, **Create Public Threads**, and **Send Messages in Threads**. Save its token in the GitHub Actions secret `DISCORD_BOT_TOKEN`.
- Set `DISCORD_THREAD_MODE: forum` if the webhook belongs to a Discord forum channel. The webhook creates a forum post; no bot token is needed.

Thread modes also post a report template. Never commit webhook URLs or bot tokens.

Ensure your repository's **Actions → General → Workflow permissions** allow the GitHub token to write contents, and that branch protection does not block the bot's state commits. The workflow explicitly requests `contents: write`.

## 3. Check that everything works

In the repo, open **Actions → Announce openOMSI releases on Discord → Run workflow**.

- **`preview_latest`**: prints the latest embed JSON to the Actions log, without sending anything or modifying state.
- **`test_webhook`**: in the default mode, sends one marked TEST announcement, no thread and no state change. Delete the test announcement afterward. Thread modes also create a test discussion.
- **`check`**: checks new official releases and sends only the ones not yet announced.

Normal operation is automatic via the scheduler after setup. The first normal run may report `No new releases to announce` until a version newer than v0.3.45 appears.

## Customization

Edit `.github/workflows/announce.yml`:

- `SOURCE_REPOSITORY`: source GitHub repository (`owner/repo`). Default: `openOMSI-org/openOMSI`.
- `ANNOUNCE_PRERELEASES`: `true` (default) or `false` to ignore beta/pre-releases.
- `DISCORD_THREAD_MODE`: `webhook` (default, one message), `text` (bot + thread), or `forum` (forum post).
- `DISCORD_REPORT_CHANNEL_ID`: `1554233571764346940` by default; shown as a channel mention in webhook-only announcements.
- `cron`: schedule. GitHub supports 5-minute minimum intervals; actual runs can be delayed.

If you change the source repository, also reset `state/announced.json` to a suitable baseline to avoid posting unrelated old versions.

## Reliability and limits

- In webhook-only mode the release is marked announced after Discord accepts the message. Optional thread modes checkpoint announcement, thread creation and template delivery separately. A state push failure or interrupted network response can still cause **duplicate announcements**.
- Discord webhook and GitHub API/network failures cause an unsuccessful Actions run, rather than silently dropping the release. Transient Discord errors receive limited retries.
- The bot handles up to 5 new releases per check. Backlog beyond that rolls over to the next run.
- Discord embed limits require long release notes to be abridged (up to seven excerpted bullet points) and link to the complete original changelog.
- When using optional thread modes, manually deleting a report thread can require state repair on retry.
- GitHub can delay or skip scheduled jobs under load. In public repos, scheduled workflows may be disabled after **60 days of no repository activity**; re-enable via the Actions tab if that happens.
- For a stronger exactly-once guarantee, use a transactional external store or webhook receiver. This repository prioritizes simplicity and zero infrastructure.

## Run local tests

```bash
python -m unittest discover -s tests -v
```

## Repository layout

```text
.github/workflows/announce.yml  # scheduler, manual check and webhook test
scripts/announce_release.py     # Release polling and retry-state checkpoints
scripts/discord_delivery.py     # Rich embeds, Discord REST API and report threads
tests/test_threads.py           # Thread and retry tests
tests/test_announcer.py         # offline unit tests
state/announced.json            # tracked announcement history/baseline
setup.ps1                       # create/push the repo from Windows
```

## Security

The webhook is never stored in code or Actions logs. Discord mentions are disabled in the message payload. The script validates that the webhook is an HTTPS `discord.com` endpoint and uses a restricted GitHub Actions token scoped to repository contents writes.
