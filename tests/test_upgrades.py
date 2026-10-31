"""Offline tests of V3 release formatting, updates, diagnostics, and state migration."""
import importlib.util
import json
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError

ROOT = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(ROOT))
import discord_delivery as delivery

spec = importlib.util.spec_from_file_location("announcer_upgrades", ROOT / "announce_release.py")
a = importlib.util.module_from_spec(spec)
spec.loader.exec_module(a)

WEBHOOK = "https://discord.com/api/webhooks/123456789012345678/secretisnotprinted"
MESSAGE_ID = "987654321098765432"


def release(id=200, body="### Fixes\n- Fixed stuttering", prerelease=False):
    return {
        "id": id, "tag_name": f"v0.3.{id}", "name": f"openOMSI 0.3.{id}",
        "published_at": f"2026-10-{20 if id == 199 else 29}T12:00:00Z",
        "html_url": f"https://github.com/openOMSI-org/openOMSI/releases/tag/v0.3.{id}",
        "body": body, "prerelease": prerelease, "draft": False,
        "assets": [
            {"id": 1, "name": f"openOMSI-0.3.{id}-windows-x64-setup.exe", "size": 23456,
             "browser_download_url": f"https://github.com/openOMSI-org/openOMSI/releases/download/v0.3.{id}/win.exe"},
            {"id": 2, "name": f"openOMSI-0.3.{id}-android-arm64.apk", "size": 234,
             "browser_download_url": f"https://github.com/openOMSI-org/openOMSI/releases/download/v0.3.{id}/phone.apk"},
        ],
    }


class UpgradeTests(unittest.TestCase):
    def test_platform_downloads_real_assets_only(self):
        r = release()
        p = delivery.make_payload(r, "openOMSI-org/openOMSI", previous_tag="v0.3.199")
        values = {f["name"]: f["value"] for f in p["embeds"][0]["fields"]}
        self.assertIn("🪟 Windows", values)
        self.assertIn("📱 Android", values)
        self.assertNotIn("🐧 Linux", values)
        self.assertNotIn("🍎 macOS", values)
        self.assertIn("win.exe", values["🪟 Windows"])
        self.assertIn("/compare/v0.3.199...v0.3.200", values["🔍 COMPARE"])
        self.assertIn("<#1554233571764346940>", values["🐛 FOUND A BUG?"])
        self.assertEqual(p["allowed_mentions"], {"parse": []})

    def test_changelog_categories_preserve_author_words(self):
        r = release(body="### New Features\n- Added gamepad mapping\n### Fixes\n- Fixed stuttering\n### Known Issues\n- Multiplayer still unstable\n### Downloads\n- Windows")
        text = delivery.make_payload(r, "openOMSI-org/openOMSI")["embeds"][0]["description"]
        self.assertIn("New Features", text)
        self.assertIn("Bug Fixes", text)
        self.assertIn("Known Issues", text)
        self.assertIn("Multiplayer still unstable", text)
        self.assertNotIn("- Windows", text)
        self.assertLessEqual(len(text), 4096)

    def test_realistic_release_fits_all_discord_limits(self):
        r = release(body="### Fixes\n" + "\n".join(f"- Fixed complex case {n} " + "very " * 50 for n in range(30)))
        p = delivery.make_payload(r, "openOMSI-org/openOMSI")
        embed = p["embeds"][0]
        self.assertLessEqual(len(embed["description"]), 4096)
        self.assertTrue(all(len(x["value"]) <= 1024 for x in embed["fields"]))
        total = sum(len(x.get("name", "")) + len(x.get("value", "")) for x in embed["fields"])
        self.assertLessEqual(total + len(embed["description"]) + len(embed["title"]), 6000)

    def test_many_long_download_urls_stay_within_embed_total_limit(self):
        r = release(body="### Improvements\n" + "\n".join(f"- Improved item {n} " + "x" * 250 for n in range(20)))
        for category in ("windows", "macos", "linux", "android", "server"):
            for idx in range(5):
                r["assets"].append({"id": len(r["assets"]) + 1,
                                    "name": f"openOMSI-{category}-x64-{idx}.zip",
                                    "size": 400,
                                    "browser_download_url": "https://github.com/openOMSI-org/openOMSI/releases/download/v0.3.200/" + "x" * 230 + f"-{category}-{idx}.zip"})
        embed = delivery.make_payload(r, "openOMSI-org/openOMSI")["embeds"][0]
        size = (len(embed["title"]) + len(embed["description"]) +
                sum(len(f["name"]) + len(f["value"]) for f in embed["fields"]) +
                len(embed["footer"]["text"]) + len(embed["author"]["name"]))
        self.assertLessEqual(size, 6000)
        self.assertTrue(all(len(f["value"]) <= 1024 for f in embed["fields"]))

    def test_server_downloads_are_distinguishable_and_complete(self):
        r = release()
        base = "https://github.com/openOMSI-org/openOMSI/releases/download/v0.3.200/"
        names = ["server-windows-arm64.zip", "server-linux-x64.zip", "server-windows-x64.zip",
                 "server-linux-arm64.zip", "macos-arm64.zip", "macos-x64.zip"]
        r["assets"] += [{"id": 10 + i, "name": f"openOMSI-0.3.200-{n}", "size": 1,
                         "browser_download_url": base + f"openOMSI-0.3.200-{n}"} for i, n in enumerate(names)]
        values = {f["name"]: f["value"] for f in delivery.make_payload(r, "openOMSI-org/openOMSI")["embeds"][0]["fields"]}
        labels = re.findall(r"\[([^\]]+)\]", values["🖧 Dedicated Server"])
        self.assertEqual(labels, ["Linux x64 ZIP", "Linux ARM64 ZIP", "Windows x64 ZIP", "Windows ARM64 ZIP"])
        self.assertEqual(re.findall(r"\[([^\]]+)\]", values["🍎 macOS"]), ["Intel ZIP", "Apple Silicon ZIP"])

    def test_intro_known_followups_and_compact_pr_links(self):
        body = ("**openOMSI 0.2.60**\n\n> **Beta, for testing.** Report what breaks.\n\n"
                "### New\n- Update channel ([#2571](https://github.com/openOMSI-org/openOMSI/pull/2571)).\n"
                "- Fixed in (see [#9](https://github.com/openOMSI-org/openOMSI/pull/9))\n"
                "### Project and security checks\n- CodeQL analyses workflows\n"
                "### Known follow-ups\n- Switching channels can race\n")
        text = delivery.format_changelog(body, "https://github.com/openOMSI-org/openOMSI/releases/tag/v1")
        self.assertTrue(text.startswith("> **Beta, for testing.** Report what breaks."))
        self.assertIn("Update channel (#2571).", text)
        self.assertIn("Fixed in (see #9)", text)
        self.assertNotIn("/pull/", text)
        self.assertIn("**🔒 Security & Project**\n• CodeQL", text)
        self.assertIn("**⚠️ Known Issues**\n• Switching channels can race", text)
        self.assertNotIn("Other Changes", text)

    def test_release_date_uses_discord_timestamps(self):
        values = {f["name"]: f["value"] for f in delivery.make_payload(release(), "openOMSI-org/openOMSI")["embeds"][0]["fields"]}
        self.assertEqual(values["📅 RELEASED"], "<t:1793275200:D>\n<t:1793275200:R>")

    def test_fingerprint_ignores_non_message_changes(self):
        r = release()
        fp = a.release_fingerprint(r, "v0.3.199")
        r["updated_at"] = "2026-11-01T00:00:00Z"
        self.assertEqual(fp, a.release_fingerprint(r, "v0.3.199"))
        r["assets"].append({"id": 3, "name": "openOMSI-linux-x64.zip", "size": 100,
                             "browser_download_url": "https://github.com/openOMSI-org/openOMSI/releases/download/v0.3.200/linux.zip"})
        self.assertNotEqual(fp, a.release_fingerprint(r, "v0.3.199"))

    def test_previous_tag_uses_published_order(self):
        earlier, newer = release(id=199), release(id=200)
        self.assertEqual(a.previous_tag(newer, [newer, earlier]), "v0.3.199")

    def test_webhook_message_recorded_for_edits(self):
        state = {"announced_release_ids": [], "messages": {}, "pending_threads": {}}
        with patch.object(a, "send_discord", return_value={"id": MESSAGE_ID, "channel_id": "111111111111111111"}) as sent, \
             patch.object(a, "write_and_push_state") as write:
            a.deliver_release(release(), "openOMSI-org/openOMSI", WEBHOOK, "webhook", "", state, prior_tag="v0.3.199")
        self.assertEqual(sent.call_count, 1)
        self.assertEqual(write.call_count, 1)
        self.assertEqual(state["messages"]["200"]["message_id"], MESSAGE_ID)
        self.assertEqual(state["messages"]["200"]["webhook_id"], "123456789012345678")
        self.assertIn(200, state["announced_release_ids"])

    def test_update_edits_only_in_place(self):
        r = release()
        state = {"messages": {"200": {"message_id": MESSAGE_ID,
                                         "webhook_id": "123456789012345678",
                                         "fingerprint": a.release_fingerprint(r)}},
                 "announced_release_ids": [200]}
        with patch.object(a, "edit_discord", return_value={"id": MESSAGE_ID}) as edit, \
             patch.object(a, "send_discord") as newpost, \
             patch.object(a, "write_and_push_state") as write:
            self.assertEqual(a.sync_updated_releases([r], state, "openOMSI-org/openOMSI", WEBHOOK,
                                                      delivery.DEFAULT_REPORT_CHANNEL_ID), [])
            r["body"] += "\n- Fixed fonts"
            updated = a.sync_updated_releases([r], state, "openOMSI-org/openOMSI", WEBHOOK,
                                              delivery.DEFAULT_REPORT_CHANNEL_ID)
            self.assertEqual(updated, ["v0.3.200"])
            self.assertEqual(edit.call_count, 1)
            newpost.assert_not_called()
            write.assert_called_once()
            self.assertEqual(a.sync_updated_releases([r], state, "openOMSI-org/openOMSI", WEBHOOK,
                                                      delivery.DEFAULT_REPORT_CHANNEL_ID), [])
        self.assertEqual(state["messages"]["200"]["fingerprint"], a.release_fingerprint(r))

    def test_deleted_message_fails_without_reposting(self):
        r = release()
        state = {"messages": {"200": {"message_id": MESSAGE_ID,
                                         "webhook_id": "123456789012345678", "fingerprint": "old"}}}
        with patch.object(a, "edit_discord", side_effect=RuntimeError("Discord request failed (HTTP 404).")), \
             patch.object(a, "send_discord") as newpost:
            with self.assertRaisesRegex(RuntimeError, "HTTP 404"):
                a.sync_updated_releases([r], state, "openOMSI-org/openOMSI", WEBHOOK,
                                        delivery.DEFAULT_REPORT_CHANNEL_ID)
            newpost.assert_not_called()

    def test_changed_webhook_rejected_before_edit(self):
        r = release()
        state = {"messages": {"200": {"message_id": MESSAGE_ID,
                                         "webhook_id": "999999999999999999", "fingerprint": "old"}}}
        with patch.object(a, "edit_discord") as edit:
            with self.assertRaisesRegex(RuntimeError, "Webhook ID changed"):
                a.sync_updated_releases([r], state, "openOMSI-org/openOMSI", WEBHOOK,
                                        delivery.DEFAULT_REPORT_CHANNEL_ID)
            edit.assert_not_called()

    def test_do_not_mark_sent_without_message_id(self):
        state = {"messages": {}, "announced_release_ids": []}
        with patch.object(a, "send_discord", return_value={}), patch.object(a, "write_and_push_state") as write:
            with self.assertRaisesRegex(RuntimeError, "valid message ID"):
                a.deliver_release(release(), "openOMSI-org/openOMSI", WEBHOOK, "webhook", "", state)
            write.assert_not_called()
            self.assertEqual(state["announced_release_ids"], [])

    def test_state_migration(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "state.json"
            path.write_text(json.dumps({"baseline_published_at": "2026-10-27T00:15:47Z",
                                        "announced_release_ids": [199]}))
            state = a.read_state(path)
            self.assertEqual(state["messages"], {})
            self.assertEqual(state["pending_threads"], {})

    def test_healthcheck_does_not_post(self):
        with patch.object(a, "fetch_releases", return_value=[release()]), \
             patch.object(a, "check_discord_webhook", return_value="123456789012345678") as check, \
             patch.object(a, "send_discord") as send, \
             patch.dict(os.environ, {"WORKFLOW_MODE": "healthcheck", "DISCORD_WEBHOOK_URL": WEBHOOK}):
            self.assertEqual(a.main(), 0)
            self.assertEqual(check.call_count, 1)
            send.assert_not_called()

    def test_webhook_edit_endpoint_and_method(self):
        with patch.object(delivery, "request_json", return_value={"id": MESSAGE_ID}) as req:
            delivery.edit_discord({"embeds": [{"title": "Updated"}]}, WEBHOOK, MESSAGE_ID)
            args, kwargs = req.call_args
            self.assertTrue(args[0].endswith("/messages/" + MESSAGE_ID))
            self.assertEqual(kwargs["method"], "PATCH")
            self.assertEqual(args[1]["allowed_mentions"], {"parse": []})

    def test_no_token_appears_in_friendly_errors(self):
        with patch.object(delivery, "urlopen", side_effect=HTTPError(WEBHOOK, 403, "no", {}, None)):
            with self.assertRaisesRegex(RuntimeError, "HTTP 403") as ctx:
                delivery.check_discord_webhook(WEBHOOK)
            self.assertNotIn("secretisnotprinted", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
