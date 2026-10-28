import importlib.util
import sys
import unittest
from pathlib import Path

MODULE = Path(__file__).resolve().parents[1] / "scripts" / "announce_release.py"
sys.path.insert(0, str(MODULE.parent))
spec = importlib.util.spec_from_file_location("announcer", MODULE)
announcer = importlib.util.module_from_spec(spec)
sys.modules["announcer"] = announcer
spec.loader.exec_module(announcer)


def release(id, published_at, prerelease=False, body="### Fixes\n- Improved performance"):
    return {
        "id": id,
        "published_at": published_at,
        "prerelease": prerelease,
        "draft": False,
        "tag_name": f"v0.3.{id}",
        "html_url": f"https://github.com/openOMSI-org/openOMSI/releases/tag/v0.3.{id}",
        "body": body,
    }


class AnnouncerTests(unittest.TestCase):
    def test_only_new_and_unsent_releases(self):
        state = {"baseline_published_at": "2026-10-27T00:15:47Z", "announced_release_ids": [45, 47]}
        rows = [release(44, "2026-10-26T00:00:00Z"), release(45, "2026-10-27T00:15:47Z"),
                release(47, "2026-10-29T01:00:00Z"), release(46, "2026-10-28T00:00:00Z")]
        self.assertEqual([r["id"] for r in announcer.eligible_releases(rows, state, True)], [46])

    def test_prerelease_toggle(self):
        rows = [release(46, "2026-10-28T00:00:00Z", prerelease=True)]
        state = {"baseline_published_at": "2026-10-27T00:00:00Z", "announced_release_ids": []}
        self.assertEqual(announcer.eligible_releases(rows, state, False), [])
        self.assertEqual(len(announcer.eligible_releases(rows, state, True)), 1)

    def test_clean_changelog_and_prevent_ping(self):
        body = "### Changes\n- Fixed steering @everyone\n### Downloads\n| platform | link |"
        desc = announcer.format_changelog(body, "https://github.com/openOMSI-org/openOMSI/releases/tag/v1")
        self.assertNotIn("### Downloads", desc)
        self.assertNotIn("@everyone", desc)
        self.assertIn("Fixed steering", desc)

    def test_payload_is_embed_compatible(self):
        p = announcer.make_payload(release(48, "2026-10-29T00:00:00Z", body="A" * 10000), "openOMSI-org/openOMSI")
        self.assertLessEqual(len(p["embeds"][0]["description"]), 4096)
        self.assertEqual(p["allowed_mentions"], {"parse": []})
        self.assertIn("Full release notes", p["embeds"][0]["description"])

    def test_webhook_validation(self):
        url = "https://discord.com/api/webhooks/123/abc"
        formatted = announcer.webhook_url_with_wait(url, "123456789012345678")
        self.assertIn("wait=true", formatted)
        self.assertIn("thread_id=123456789012345678", formatted)
        with self.assertRaises(ValueError):
            announcer.webhook_url_with_wait(url + "?thread_id=123456789012345678")
        for bad in ["http://discord.com/api/webhooks/12/abc", "https://evil.org/api/webhooks/12/abc"]:
            with self.assertRaises(ValueError):
                announcer.webhook_url_with_wait(bad)


if __name__ == "__main__":
    unittest.main()
