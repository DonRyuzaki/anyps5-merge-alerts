import base64
import contextlib
import copy
import io
import json
import os
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError

import notify

WEBHOOK = "https://discord.com/api/webhooks/123456789/fake_test_secret"


def state():
    return {"version": 1, "upstream": notify.UPSTREAM, "author": notify.AUTHOR, "delivered": {},
            "pending": None, "heartbeat_at": notify.now()}


def pull(number=13910):
    return {"number": number, "title": "A title @everyone", "merged": True, "state": "closed",
            "merged_at": "2026-11-01T09:40:34Z", "user": {"login": notify.AUTHOR},
            "base": {"repo": {"full_name": notify.UPSTREAM}}}


class MemoryStore:
    def __init__(self):
        self.state = state()
        self.writes = 0

    def load(self):
        return copy.deepcopy(self.state), str(self.writes)

    def save(self, value, sha):
        self.writes += 1
        self.state = copy.deepcopy(value)
        return str(self.writes)


class DeliveryTests(unittest.TestCase):
    def setUp(self):
        self.github = Mock()
        self.github.candidates.return_value = [13910]
        self.github.pull.return_value = pull()
        self.store = MemoryStore()
        self.discord = Mock()
        self.discord.send.return_value = "12345"
        self.output = contextlib.redirect_stdout(io.StringIO())
        self.output.__enter__()
        self.addCleanup(self.output.__exit__, None, None, None)

    def test_new_merge_sends_once_and_rerun_sends_nothing(self):
        self.assertEqual(notify.run(self.github, self.store, self.discord), 1)
        self.assertEqual(self.store.state["delivered"]["13910"]["message_id"], "12345")
        self.assertIsNone(self.store.state["pending"])
        self.assertEqual(notify.run(self.github, self.store, self.discord), 0)
        self.discord.send.assert_called_once()

    def test_open_approved_and_closed_unmerged_prs_never_send(self):
        for merged, status, merged_at in ((False, "open", None), (False, "closed", None),
                                          (True, "open", "2026-11-01T09:40:34Z"), (True, "closed", None)):
            with self.subTest(merged=merged, status=status):
                value = pull()
                value.update(merged=merged, state=status, merged_at=merged_at, review_decision="APPROVED")
                self.github.pull.return_value = value
                self.assertEqual(notify.run(self.github, self.store, self.discord), 0)
        self.discord.send.assert_not_called()
        self.assertEqual(self.store.writes, 0)

    def test_other_author_repository_and_number_never_send(self):
        for change in ({"user": {"login": "someone-else"}}, {"base": {"repo": {"full_name": "other/repo"}}},
                       {"number": 1234}):
            with self.subTest(change=change):
                self.github.pull.return_value = {**pull(), **change}
                self.assertEqual(notify.run(self.github, self.store, self.discord), 0)
        self.discord.send.assert_not_called()

    def test_pending_is_persisted_before_post(self):
        def send(value):
            self.assertEqual(self.store.state["pending"]["number"], 13910)
            self.assertEqual(self.store.writes, 1)
            return "12345"
        self.discord.send.side_effect = send
        notify.run(self.github, self.store, self.discord)

    def test_failed_pending_save_prevents_any_message(self):
        self.store.save = Mock(side_effect=notify.NotificationError("storage unavailable"))
        with self.assertRaises(notify.NotificationError):
            notify.run(self.github, self.store, self.discord)
        self.discord.send.assert_not_called()

    def test_uncertain_post_blocks_retry_in_next_run(self):
        self.discord.send.side_effect = notify.NotificationError("uncertain delivery")
        with self.assertRaises(notify.NotificationError):
            notify.run(self.github, self.store, self.discord)
        with self.assertRaisesRegex(notify.NotificationError, "reconcile"):
            notify.run(self.github, self.store, self.discord)
        self.discord.send.assert_called_once()

    def test_definitive_rejection_can_retry_after_fixing_destination(self):
        self.discord.send.side_effect = notify.DeliveryRejected("HTTP 404")
        with self.assertRaises(notify.DeliveryRejected):
            notify.run(self.github, self.store, self.discord)
        self.assertIsNone(self.store.state["pending"])
        self.discord.send.side_effect = None
        self.assertEqual(notify.run(self.github, self.store, self.discord), 1)

    def test_failed_final_state_save_never_replays_confirmed_message(self):
        original = self.store.save
        def save(value, sha):
            if self.store.writes == 1:
                raise notify.NotificationError("storage unavailable")
            return original(value, sha)
        self.store.save = save
        with self.assertRaises(notify.NotificationError):
            notify.run(self.github, self.store, self.discord)
        with self.assertRaisesRegex(notify.NotificationError, "reconcile"):
            notify.run(self.github, self.store, self.discord)
        self.discord.send.assert_called_once()

    def test_dry_run_does_not_send_or_write(self):
        self.assertEqual(notify.run(self.github, self.store, self.discord, dry_run=True), 0)
        self.discord.send.assert_not_called()
        self.assertEqual(self.store.writes, 0)


class TransportTests(unittest.TestCase):
    def response(self, value):
        return io.BytesIO(json.dumps(value).encode())

    def test_webhook_payload_cannot_ping_and_waits_for_message_identity(self):
        opener = Mock(return_value=self.response({"id": "123"}))
        self.assertEqual(notify.Discord(WEBHOOK, opener=opener).send(pull()), "123")
        request = opener.call_args.args[0]
        self.assertTrue(request.full_url.endswith("?wait=true"))
        self.assertEqual(json.loads(request.data)["allowed_mentions"], {"parse": []})
        self.assertEqual(json.loads(request.data)["embeds"][0]["url"], "https://github.com/boykopovar/AnyPS5/pull/13910")

    def test_unsafe_or_missing_webhook_never_enters_transport(self):
        for url in ("", "http://discord.com/api/webhooks/1/key", "https://example.com/api/webhooks/1/key",
                    WEBHOOK + "?redirect=other", WEBHOOK + "#fragment", "https://discord.com@evil.example/api/webhooks/1/key"):
            with self.subTest(url=url), self.assertRaises(notify.NotificationError):
                notify.Discord(url)

    def test_discord_network_and_server_errors_are_not_retried_or_leaked(self):
        for error in (URLError(WEBHOOK), TimeoutError(WEBHOOK),
                      HTTPError(WEBHOOK, 500, "error", {}, io.BytesIO())):
            opener = Mock(side_effect=error)
            with self.subTest(error=type(error).__name__), self.assertRaises(notify.NotificationError) as failure:
                notify.Discord(WEBHOOK, opener=opener).send(pull())
            self.assertNotIn("fake_test_secret", str(failure.exception))
            self.assertEqual(opener.call_count, 1)

    def test_429_retries_only_confirmed_rate_limit_rejection(self):
        for body in ({"retry_after": 0}, None, []):
            opener = Mock(side_effect=[HTTPError(WEBHOOK, 429, "rate limit", {}, self.response(body)), self.response({"id": "123"})])
            with self.subTest(body=body):
                self.assertEqual(notify.Discord(WEBHOOK, opener=opener, sleeper=Mock()).send(pull()), "123")
                self.assertEqual(opener.call_count, 2)

    def test_non_confirmation_and_redirects_never_count_as_delivered(self):
        for response in ({}, {"id": 123}, {"id": "invalid"}):
            with self.subTest(response=response), self.assertRaises(notify.NotificationError):
                notify.Discord(WEBHOOK, opener=Mock(return_value=self.response(response))).send(pull())
        self.assertIsNone(notify.NoRedirect().redirect_request(None, None, 302, "", {}, "https://example.com"))

    def test_search_pagination_collects_every_merge(self):
        github = notify.GitHub("token")
        github.request = Mock(side_effect=[{"incomplete_results": False, "total_count": 101,
                                           "items": [{"number": n, "pull_request": {}} for n in range(1, 101)]},
                                          {"incomplete_results": False, "total_count": 101,
                                           "items": [{"number": 101, "pull_request": {}}]}])
        self.assertEqual(github.candidates(), list(range(1, 102)))

    def test_search_incomplete_overflow_or_changed_pages_fail_closed(self):
        for response in ({"incomplete_results": True, "total_count": 1, "items": []},
                         {"incomplete_results": False, "total_count": 1001, "items": []},
                         {"incomplete_results": False, "total_count": 1, "items": []}):
            github = notify.GitHub("token")
            github.request = Mock(return_value=response)
            with self.subTest(response=response), self.assertRaises(notify.NotificationError):
                github.candidates()


class StateTests(unittest.TestCase):
    def file(self, value, sha="original"):
        return {"sha": sha, "encoding": "base64", "content": base64.b64encode(json.dumps(value).encode()).decode()}

    def test_missing_or_corrupt_state_is_not_reset(self):
        for value in ({}, {**state(), "version": 2}, {**state(), "pending": "broken"}, {**state(), "delivered": []}):
            github = Mock()
            github.request.return_value = self.file(value)
            with self.subTest(value=value), self.assertRaises(notify.NotificationError):
                notify.StateStore(github).load()

    def test_put_with_lost_response_reconciles_exact_remote_state(self):
        value = state()
        github = Mock()
        github.request.side_effect = [notify.NotificationError("response lost"), self.file(value, "saved")]
        self.assertEqual(notify.StateStore(github).save(value, "original"), "saved")

    def test_concurrent_state_is_never_overwritten(self):
        value, other = state(), state()
        other["pending"] = {"number": 111}
        github = Mock()
        github.request.side_effect = [notify.NotificationError("409"), self.file(other, "other")]
        with self.assertRaisesRegex(notify.NotificationError, "concurrently"):
            notify.StateStore(github).save(value, "original")
        self.assertEqual(github.request.call_count, 2)

    def test_state_write_retries_stop_and_do_not_erase_history(self):
        github = Mock()
        github.request.side_effect = notify.NotificationError("server unavailable")
        with self.assertRaisesRegex(notify.NotificationError, "persisted"):
            notify.StateStore(github).save(state(), "original")
        self.assertEqual(github.request.call_count, 6)

    def test_unexpected_exception_logging_never_prints_secret(self):
        with patch.dict(os.environ, {"GITHUB_REPOSITORY": notify.OWNER, "GH_TOKEN": "token", "DISCORD_WEBHOOK_URL": WEBHOOK}), \
                patch.object(notify, "run", side_effect=RuntimeError(WEBHOOK)), \
                contextlib.redirect_stderr(io.StringIO()) as output:
            self.assertEqual(notify.main(), 1)
        self.assertNotIn("fake_test_secret", output.getvalue())


if __name__ == "__main__":
    unittest.main()
