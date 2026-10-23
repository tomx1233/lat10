"""Tests for the /copyr8 Discord delivery + live progress helpers.

audio_bot.py needs discord.py, a token and the whole bot runtime to import, so
(like test_copyr8_macro.BotRoutingTests) the helpers under test are extracted
from the source with `ast` and executed against a small fake `discord` module.
"""
import ast
import asyncio
import datetime
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent

HELPERS = (
    "COPYR8_PROGRESS_REFRESH_SECONDS", "_DISCORD_FATAL_STATUSES",
    "_format_wait", "_copyr8_pacing_line", "_copyr8_progress_content",
    "_update_copyr8_progress", "_safe_followup_send", "_safe_channel_send",
    "_deliver_result", "_interaction_token_expired",
    "_FOLLOWUP_MAX_AGE_SECONDS",
)


class FakeHTTPException(Exception):
    def __init__(self, status, text="error"):
        super().__init__(f"{status} {text}")
        self.status = status


class FakeForbidden(FakeHTTPException):
    def __init__(self, text="Missing Access"):
        super().__init__(403, text)


def load_helpers(pacing_status=None):
    tree = ast.parse((ROOT / "audio_bot.py").read_text())
    nodes = []
    for node in tree.body:
        name = getattr(node, "name", None)
        if name is None and isinstance(node, ast.Assign):
            name = getattr(node.targets[0], "id", None)
        if name in HELPERS:
            nodes.append(node)
    found = {getattr(n, "name", None) or n.targets[0].id for n in nodes}
    missing = set(HELPERS) - found
    if missing:
        raise AssertionError(f"helpers missing from audio_bot.py: {missing}")
    log = []
    ns = {
        "asyncio": asyncio,
        "datetime": datetime,
        "discord": types.SimpleNamespace(HTTPException=FakeHTTPException,
                                         Forbidden=FakeForbidden),
        "killa_tools": types.SimpleNamespace(pacing_status=pacing_status),
        "_log": log.append,
    }
    exec(compile(ast.Module(body=nodes, type_ignores=[]), "audio_bot.py", "exec"), ns)
    ns["LOG"] = log
    return ns


class Sender:
    """Records calls; raises the queued exception (if any) on each call."""

    def __init__(self, *errors):
        self.errors = list(errors)
        self.calls = []

    async def send(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        if self.errors:
            err = self.errors.pop(0)
            if err is not None:
                raise err


def interaction(followup, channel, user=None, created_at=None):
    return types.SimpleNamespace(
        followup=followup, channel=channel, created_at=created_at,
        user=user or types.SimpleNamespace(mention="<@1>", send=Sender().send))


class DeliveryTests(unittest.TestCase):
    def setUp(self):
        self.ns = load_helpers()

    def run_async(self, coro):
        return asyncio.run(coro)

    def test_followup_success_does_not_touch_channel(self):
        fu, ch = Sender(), Sender()
        ok = self.run_async(self.ns["_deliver_result"](
            interaction(fu, ch), lambda: {"embed": "E"}))
        self.assertTrue(ok)
        self.assertEqual(len(fu.calls), 1)
        self.assertEqual(ch.calls, [])

    def test_expired_token_falls_back_to_channel_with_fresh_files(self):
        built = []

        def make():
            built.append(object())      # a new "discord.File" every call
            return {"embed": "E", "files": [built[-1]]}

        fu = Sender(FakeHTTPException(401, "Invalid Webhook Token"))
        ch = Sender()
        ok = self.run_async(self.ns["_deliver_result"](interaction(fu, ch), make))
        self.assertTrue(ok)
        self.assertEqual(len(ch.calls), 1)
        sent_files = ch.calls[0][1]["files"]
        self.assertIsNot(sent_files[0], fu.calls[0][1]["files"][0])
        self.assertIn("<@1>", ch.calls[0][1]["content"])

    def test_401_then_403_never_raises_and_tries_dm(self):
        dm = Sender()
        user = types.SimpleNamespace(mention="<@1>", send=dm.send)
        fu = Sender(FakeHTTPException(401))
        ch = Sender(FakeForbidden())
        ok = self.run_async(self.ns["_deliver_result"](
            interaction(fu, ch, user), lambda: {"embed": "E"}))
        self.assertTrue(ok)
        self.assertEqual(len(dm.calls), 1)
        self.assertTrue(any("401" in line for line in self.ns["LOG"]))
        self.assertTrue(any("403" in line for line in self.ns["LOG"]))

    def test_expired_interaction_skips_the_guaranteed_401(self):
        fu, ch = Sender(), Sender()
        old = (datetime.datetime.now(datetime.timezone.utc)
               - datetime.timedelta(minutes=20))
        ok = self.run_async(self.ns["_deliver_result"](
            interaction(fu, ch, created_at=old), lambda: {"embed": "E"}))
        self.assertTrue(ok)
        self.assertEqual(fu.calls, [])          # no guaranteed-401 attempt
        self.assertEqual(len(ch.calls), 1)
        self.assertTrue(any("15-minute" in line for line in self.ns["LOG"]))
        self.assertTrue(any("channel fallback" in line
                            for line in self.ns["LOG"]))

    def test_fresh_interaction_still_tries_the_followup(self):
        fu, ch = Sender(), Sender()
        fresh = (datetime.datetime.now(datetime.timezone.utc)
                 - datetime.timedelta(minutes=5))
        ok = self.run_async(self.ns["_deliver_result"](
            interaction(fu, ch, created_at=fresh), lambda: {"embed": "E"}))
        self.assertTrue(ok)
        self.assertEqual(len(fu.calls), 1)
        self.assertEqual(ch.calls, [])

    def test_dm_success_is_logged(self):
        dm = Sender()
        user = types.SimpleNamespace(mention="<@1>", id=42, send=dm.send)
        ok = self.run_async(self.ns["_deliver_result"](
            interaction(Sender(FakeHTTPException(401)), Sender(FakeForbidden()),
                        user),
            lambda: {"embed": "E"}))
        self.assertTrue(ok)
        self.assertTrue(any("delivered via DM to user id 42" in line
                            for line in self.ns["LOG"]))

    def test_every_route_failing_returns_false_without_raising(self):
        dm = Sender(FakeForbidden("Cannot send messages to this user"))
        user = types.SimpleNamespace(mention="<@1>", send=dm.send)
        ok = self.run_async(self.ns["_deliver_result"](
            interaction(Sender(FakeHTTPException(401)), Sender(FakeForbidden()), user),
            lambda: {"embed": "E"}))
        self.assertFalse(ok)

    def test_safe_channel_send_handles_missing_channel(self):
        self.assertFalse(self.run_async(self.ns["_safe_channel_send"](None, "x")))


class FakeMessage:
    def __init__(self, *errors):
        self.errors = list(errors)
        self.edits = []

    async def edit(self, content):
        if self.errors:
            err = self.errors.pop(0)
            if err is not None:
                raise err
        self.edits.append(content)


class ProgressTests(unittest.TestCase):
    def setUp(self):
        self.ns = load_helpers()

    def test_refresh_interval_is_one_and_a_half_seconds(self):
        self.assertEqual(self.ns["COPYR8_PROGRESS_REFRESH_SECONDS"], 1.5)

    def test_rate_limit_countdown_and_pacing_lines(self):
        line = self.ns["_copyr8_pacing_line"]
        self.assertIn("resets in **1m 05s**", line({"rate_limit_remaining": 65.2,
                                                   "consecutive_limits": 2}))
        self.assertIn("throttle #2", line({"rate_limit_remaining": 5,
                                            "consecutive_limits": 2}))
        paced = line({"rate_limit_remaining": 0.0, "breath_remaining": 3.24,
                      "breath_seconds": 6.0})
        self.assertIn("next probe in **3.2s**", paced)
        self.assertEqual(line({"rate_limit_remaining": 0.0,
                               "breath_remaining": 0.1}), "")
        self.assertEqual(line(None), "")

    def _run_updater(self, msg, statuses, ticks):
        lines = ["testing +0.25 st / +1.0%..."]
        it = iter(statuses)

        def status_fn():
            return next(it, statuses[-1])

        async def go():
            task = asyncio.create_task(self.ns["_update_copyr8_progress"](
                msg, lines, "CopyR8 Auto-Fit V3", status_fn=status_fn,
                interval=0.01))
            await asyncio.sleep(0.01 * ticks + 0.02)
            done = task.done()
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            return done

        return asyncio.run(go())

    def test_countdown_ticks_live_and_skips_identical_edits(self):
        msg = FakeMessage()
        statuses = [{"rate_limit_remaining": 30.0}, {"rate_limit_remaining": 30.0},
                    {"rate_limit_remaining": 28.4}, {"rate_limit_remaining": 0.0,
                                                     "breath_remaining": 0.0}]
        self._run_updater(msg, statuses, ticks=6)
        self.assertEqual(len(msg.edits), 3)          # 30s, 28s, cleared
        self.assertIn("resets in **30s**", msg.edits[0])
        self.assertIn("resets in **28s**", msg.edits[1])
        self.assertNotIn("\n", msg.edits[2])
        self.assertTrue(msg.edits[0].startswith("CopyR8 Auto-Fit V3: testing"))

    def test_expired_token_stops_updates_quietly(self):
        for status in (401, 403, 404):
            with self.subTest(status=status):
                msg = FakeMessage(FakeHTTPException(status))
                finished = self._run_updater(
                    msg, [{"rate_limit_remaining": 10.0 + i} for i in range(9)],
                    ticks=8)
                self.assertTrue(finished, "updater must exit on a fatal status")
                self.assertEqual(msg.edits, [])

    def test_transient_errors_keep_updating(self):
        msg = FakeMessage(FakeHTTPException(503), RuntimeError("reset"))
        finished = self._run_updater(msg, [{"rate_limit_remaining": 9.0}], ticks=6)
        self.assertFalse(finished)
        self.assertEqual(len(msg.edits), 1)

    def test_status_errors_never_kill_the_updater(self):
        msg = FakeMessage()

        def boom():
            raise RuntimeError("status unavailable")

        async def go():
            task = asyncio.create_task(self.ns["_update_copyr8_progress"](
                msg, ["step"], "Auto-fit", status_fn=boom, interval=0.01))
            await asyncio.sleep(0.05)
            alive = not task.done()
            task.cancel()
            return alive

        self.assertTrue(asyncio.run(go()))
        self.assertEqual(msg.edits, ["Auto-fit: step"])


class PacingStatusTests(unittest.TestCase):
    def test_reports_live_rate_limit_and_breath(self):
        import os
        import sys
        sys.path.insert(0, str(ROOT))
        os.environ.setdefault("ROBLOX_COOKIE", "")
        import killa_tools as kt
        saved = (kt._rate_limit_until, kt._last_post_time, kt._breath_seconds,
                 kt._consecutive_limits)
        try:
            kt._rate_limit_until = 1000.0 + 42.0
            kt._last_post_time = 1000.0 - 2.0
            kt._breath_seconds = 6.0
            kt._consecutive_limits = 2
            st = kt.pacing_status(now=1000.0)
            self.assertTrue(st["rate_limited"])
            self.assertAlmostEqual(st["rate_limit_remaining"], 42.0)
            self.assertAlmostEqual(st["breath_remaining"], 4.0)
            self.assertEqual(st["consecutive_limits"], 2)
            st = kt.pacing_status(now=2000.0)
            self.assertFalse(st["rate_limited"])
            self.assertEqual(st["breath_remaining"], 0.0)
        finally:
            (kt._rate_limit_until, kt._last_post_time, kt._breath_seconds,
             kt._consecutive_limits) = saved


if __name__ == "__main__":
    unittest.main()
