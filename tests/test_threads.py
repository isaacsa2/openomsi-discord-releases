import importlib.util
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1] / 'scripts'
sys.path.insert(0, str(ROOT))
from discord_delivery import (format_changelog, make_payload, report_template,
                              report_thread_name, webhook_url_with_wait)

spec = importlib.util.spec_from_file_location('announcer_threads', ROOT / 'announce_release.py')
a = importlib.util.module_from_spec(spec)
spec.loader.exec_module(a)


def release():
    return {
        'id': 52, 'published_at': '2026-10-29T12:00:00Z', 'prerelease': False,
        'tag_name': 'v0.3.52', 'html_url': 'https://github.com/openOMSI-org/openOMSI/releases/tag/v0.3.52',
        'body': '### Fixes\n- Improved rendering performance\n- Fixed steering\n### Downloads\n- Windows'
    }


class ThreadTests(unittest.TestCase):
    def test_pretty_embed_and_excerpt(self):
        r = release()
        p = make_payload(r, 'openOMSI-org/openOMSI')
        self.assertEqual(p['allowed_mentions'], {'parse': []})
        self.assertIn('WHAT\'S NEW', p['embeds'][0]['description'])
        self.assertIn('Improved rendering', p['embeds'][0]['description'])
        self.assertNotIn('- Windows', p['embeds'][0]['description'])
        self.assertLessEqual(len(p['embeds'][0]['description']), 4096)
        self.assertIn('FOUND A BUG', p['embeds'][0]['fields'][-1]['name'])

    def test_forum_mode_creates_post(self):
        p = make_payload(release(), 'openOMSI-org/openOMSI', forum=True)
        self.assertEqual(p['thread_name'], '🐛 Bug Reports • openOMSI v0.3.52')

    def test_report_template_fits_and_contains_fields(self):
        template = report_template('v0.3.52', release()['html_url'])
        self.assertIn('Steps to reproduce', template['content'])
        self.assertIn('Expected behavior', template['content'])
        self.assertLessEqual(len(template['content']), 2000)
        self.assertEqual(template['allowed_mentions'], {'parse': []})

    def test_malicious_mentions_escaped(self):
        desc = format_changelog('### Fixes\n- ping @everyone <@123456789012345678>', release()['html_url'])
        self.assertNotIn('@everyone', desc)
        self.assertNotIn('<@123456789012345678>', desc)

    def test_webhook_only_payload_points_to_git_and_discord(self):
        payload = make_payload(release(), "openOMSI-org/openOMSI")
        value = payload["embeds"][0]["fields"][-1]["value"]
        self.assertIn("https://github.com/openOMSI-org/openOMSI/issues/new/choose", value)
        self.assertIn("<#1554233571764346940>", value)
        self.assertNotIn("thread attached", value)
        self.assertNotIn("thread_name", payload)

    def test_webhook_only_does_not_create_thread(self):
        state = {"announced_release_ids": [], "pending_threads": {}}
        with patch.object(a, "send_discord", return_value={"id": "123456789012345678", "channel_id": "987654321098765432"}) as sent, \
             patch.object(a, "start_report_thread") as thread, \
             patch.object(a, "write_and_push_state") as write:
            result = a.deliver_release(release(), "openOMSI-org/openOMSI", "https://discord.com/api/webhooks/123/abc", "webhook", "", state)
        self.assertIsNone(result)
        self.assertEqual(sent.call_count, 1)
        thread.assert_not_called()
        write.assert_called_once()
        self.assertEqual(state["announced_release_ids"], [52])
        self.assertFalse(state["pending_threads"])

    def test_webhook_only_finalizes_pending_thread_without_reposting(self):
        state = {"announced_release_ids": [], "pending_threads": {"52": {"message_id": "123456789012345678"}}}
        with patch.object(a, "send_discord") as sent, \
             patch.object(a, "start_report_thread") as thread, \
             patch.object(a, "write_and_push_state"):
            a.deliver_release(release(), "openOMSI-org/openOMSI", "unused", "webhook", "", state)
        sent.assert_not_called()
        thread.assert_not_called()
        self.assertEqual(state["announced_release_ids"], [52])
        self.assertFalse(state["pending_threads"])

    def test_text_channel_creates_one_thread_and_report(self):
        r = release()
        with patch.object(a, 'send_discord', side_effect=[
            {'id': '123456789012345678', 'channel_id': '987654321098765432'},
            {'id': '123456789012345679', 'channel_id': '123456789012345678'}
        ]) as webhook, patch.object(a, 'start_report_thread', return_value='123456789012345678') as thread:
            result = a.deliver_release(r, 'openOMSI-org/openOMSI', 'unused', 'text', 'fake', test=True)
        self.assertEqual(result, '123456789012345678')
        self.assertEqual(webhook.call_count, 2)
        self.assertEqual(webhook.call_args.kwargs['thread_id'], result)
        self.assertEqual(thread.call_count, 1)

    def test_forum_channel_needs_no_bot(self):
        r = release()
        with patch.object(a, 'send_discord', side_effect=[
            {'id': '123456789012345678', 'channel_id': '987654321098765432'},
            {'id': '123456789012345679', 'channel_id': '987654321098765432'}
        ]) as webhook, patch.object(a, 'start_report_thread') as thread:
            result = a.deliver_release(r, 'openOMSI-org/openOMSI', 'unused', 'forum', '', test=True)
        self.assertEqual(result, '987654321098765432')
        self.assertEqual(webhook.call_args_list[0].args[0]['thread_name'], report_thread_name(r['tag_name'], test=True))
        thread.assert_not_called()

    def test_failed_thread_creation_resume_without_repost(self):
        state = {'announced_release_ids': [], 'pending_threads': {}}
        r = release()
        with patch.object(a, 'send_discord', return_value={'id': '123456789012345678', 'channel_id': '987654321098765432'}) as send, \
             patch.object(a, 'start_report_thread', side_effect=RuntimeError('HTTP 403')) as thread, \
             patch.object(a, 'write_and_push_state'):
            with self.assertRaisesRegex(RuntimeError, 'HTTP 403'):
                a.deliver_release(r, 'openOMSI-org/openOMSI', 'unused', 'text', 'token', state)
            self.assertEqual(send.call_count, 1)
            self.assertIn('52', state['pending_threads'])
        with patch.object(a, 'send_discord', return_value={'id': '123456789012345679'}) as send, \
             patch.object(a, 'start_report_thread', return_value='123456789012345678'), \
             patch.object(a, 'write_and_push_state'):
            a.deliver_release(r, 'openOMSI-org/openOMSI', 'unused', 'text', 'token', state)
            self.assertEqual(send.call_count, 1)  # Only the report message, not another announcement.
        self.assertEqual(state['announced_release_ids'], [52])
        self.assertNotIn('52', state['pending_threads'])


if __name__ == '__main__':
    unittest.main()
