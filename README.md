# openOMSI Discord Release Announcer

A lightweight, automated Discord release announcement feed for the **official** [`openOMSI-org/openOMSI`](https://github.com/openOMSI-org/openOMSI) GitHub releases.

- Checks for new GitHub releases every ~5 minutes with GitHub Actions (schedules are best-effort, not instant).
- Publishes **stable releases and pre-releases**, but never drafts.
- Copies the **official release changelog** (normally authored in English), without inventing entries or using an AI API.
- Sends a polished Discord embed with a compact excerpt of the official changelog and official download links.
- **Automatically creates a per-release bug-report thread** and posts an English reporting template.
- Supports text / announcement channels (bot token required for thread creation) and forum channels (webhook-only posts).
- Saves progress during delivery to resume a failed thread without repeating the announcement.
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

## 2a. Enable bug-report threads (required)

Choose a Discord channel type and edit `DISCORD_THREAD_MODE` in `.github/workflows/announce.yml`:

**Regular text or announcement channel (default: `text`):** Create a bot in the [Discord Developer Portal](https://discord.com/developers/applications), invite it with the bot scope, and grant it **View Channel**, **Read Message History**, **Create Public Threads**, and **Send Messages in Threads** in the announcement channel. Create an additional GitHub Actions repository secret named **`DISCORD_BOT_TOKEN`**, containing the bot token. No Message Content intent or always-online process is needed; the Action uses the token solely to create threads via Discord's REST API. Never commit or share the token.

**Discord forum channel (`forum`):** Create a webhook inside the forum channel and change the workflow to `DISCORD_THREAD_MODE: forum`. No bot token is required: the webhook opens one post per release named `🐛 Bug Reports • openOMSI vX.Y.Z`.

Each thread receives a report template for OS/platform, steps to reproduce, expected/actual behavior, and evidence. This collects Discord community reports; it does **not** automatically create GitHub issues. Without the appropriate credentials or permissions, the Action reports an error instead of silently skipping the thread.

Ensure your repository's **Actions → General → Workflow permissions** allow the GitHub token to write contents, and that branch protection does not block the bot's state commits. The workflow explicitly requests `contents: write`.

## 3. Check that everything works

In the repo, open **Actions → Announce openOMSI releases on Discord → Run workflow**.

- **`preview_latest`**: prints the latest embed JSON to the Actions log, without sending anything or modifying state.
- **`test_webhook`**: sends an explicitly marked TEST announcement **and a TEST bug-report thread**, without changing state. Delete the test discussion afterward.
- **`check`**: checks new official releases and sends only the ones not yet announced.

Normal operation is automatic via the scheduler after setup. The first normal run may report `No new releases to announce` until a version newer than v0.3.45 appears.

## Customization

Edit `.github/workflows/announce.yml`:

- `SOURCE_REPOSITORY`: source GitHub repository (`owner/repo`). Default: `openOMSI-org/openOMSI`.
- `ANNOUNCE_PRERELEASES`: `true` (default) or `false` to ignore beta/pre-releases.
- `DISCORD_THREAD_MODE`: `text` (default, requires bot secret) or `forum` (webhook only).
- `cron`: schedule. GitHub supports 5-minute minimum intervals; actual runs can be delayed.

If you change the source repository, also reset `state/announced.json` to a suitable baseline to avoid posting unrelated old versions.

## Reliability and limits

- The bot checkpoints announcement, thread creation, and template delivery in `state/announced.json`, then marks the release as sent. Normal retries resume instead of reposting the main announcement. If a push fails or Discord accepts a request but the connection dies before state is saved, **duplicates are still possible**.
- Discord webhook and GitHub API/network failures cause an unsuccessful Actions run, rather than silently dropping the release. Transient Discord errors receive limited retries.
- The bot handles up to 5 new releases per check. Backlog beyond that rolls over to the next run.
- Discord embed limits require long release notes to be abridged (up to seven excerpted bullet points) and link to the complete original changelog.
- If a report thread is manually deleted, retries may require state repair.
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
