import datetime as dt
import io
import json
import os
import plistlib
import random
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unicodedata
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest import mock

import noob
import noob_card
import noob_reset_card


class NoobTest(unittest.TestCase):
    def setUp(self):
        prices = mock.patch.object(noob, "refresh_prices", return_value=(None, None))
        self.price_refresh = prices.start()
        self.addCleanup(prices.stop)
        # A developer's real key must never turn a test into a network call.
        environment = mock.patch.dict(os.environ)
        environment.start()
        os.environ.pop("DEEPSEEK_API_KEY", None)
        # Unit tests must never fast-forward the checkout they run from.
        os.environ["NOOB_NO_UPDATE"] = "1"
        # Keep the tests on the live-scan path; ledger tests set up their own store.
        os.environ["NOOB_NO_USAGE_CAPTURE"] = "1"
        self.addCleanup(environment.stop)
        # Never let a test read the developer's real macOS Keychain item.
        self.real_keychain_secret = noob.keychain_secret
        keychain = mock.patch.object(noob, "keychain_secret", return_value=None)
        keychain.start()
        self.addCleanup(keychain.stop)
        # Never let a test shell out to the developer's real OpenCode service.
        self.real_opencode_usage = noob.opencode_usage
        opencode = mock.patch.object(noob, "opencode_usage", return_value={})
        opencode.start()
        self.addCleanup(opencode.stop)
        # DeepSeek prices and the FX rate are fetched at launch; keep tests offline.
        self.real_fetch_usd_cny = noob.fetch_usd_cny
        self.real_deepseek_prices = noob.deepseek_prices
        self.real_fetch_deepseek_prices = noob.fetch_deepseek_prices
        self.real_fetch_deepseek_models = noob.fetch_deepseek_models
        self.real_load_deepseek_models = noob.load_deepseek_models
        self.real_save_deepseek_models = noob.save_deepseek_models
        for name, value in (("fetch_usd_cny", 7.1),
                            ("deepseek_prices", dict(noob.FALLBACK_DEEPSEEK_RATES)),
                            ("fetch_deepseek_models", None),
                            ("load_deepseek_models", {}),
                            ("save_deepseek_models", None)):
            patcher = mock.patch.object(noob, name, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_quota_deduplicates_legacy_bucket_and_formats_windows(self):
        bucket = {"limitId": "codex", "primary": {"usedPercent": 25, "windowDurationMins": 300,
                "resetsAt": 1700000000}, "secondary": {"usedPercent": 80, "windowDurationMins": 10080,
                "resetsAt": 1700000010}}
        rows = noob.quota_rows({"rateLimits": bucket, "rateLimitsByLimitId": {"codex": bucket}})
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0][:3], ("codex", "5h", 25))
        self.assertEqual(rows[1][:3], ("codex", "7d", 80))
        self.assertEqual((rows[0][4], rows[1][4]), (300, 10080))
        self.assertEqual(noob.card_quotas({"rateLimitsByLimitId": {"codex": bucket}}),
                         {300: (75, "32"), 10080: (20, "33")})

    def test_even_pace_marker_uses_time_left_in_each_window(self):
        now = 1_000_000
        self.assertEqual(noob.pace_marker(300, now + 300 * 60, 20, now), 19)
        self.assertEqual(noob.pace_marker(300, now + 150 * 60, 20, now), 10)
        self.assertEqual(noob.pace_marker(10080, now + 5040 * 60, 20, now), 10)
        self.assertEqual(noob.pace_marker(300, now - 1, 20, now), 0)
        self.assertIsNone(noob.pace_marker(300, None, 20, now))

        limits = {"rateLimits": {"limitId": "codex", "primary": {
            "usedPercent": 50, "windowDurationMins": 300,
            "resetsAt": now + 150 * 60}}}
        usage = noob.build_usage([])
        with mock.patch.object(noob.time, "time", return_value=now):
            wide = noob.render_home(usage, limits, width=80)
            narrow = noob.render_home(usage, limits, width=20)
        wide_lines = wide.splitlines()
        narrow_lines = narrow.splitlines()
        wide_row = next(i for i, line in enumerate(wide_lines) if "5H" in line and "▰" in line)
        narrow_bar = next(i for i, line in enumerate(narrow_lines) if "▰" in line)
        # Quota bars top out at 20 cells, so a half-used window fills 10 of them,
        # and the arrow marks the fraction of the bar's time left (50% -> 10).
        self.assertEqual(wide_lines[wide_row].count("▰") + wide_lines[wide_row].count("▱"),
                         noob.QUOTA_BAR_CELLS)
        self.assertEqual(wide_lines[wide_row + 1].index("↑")
                         - wide_lines[wide_row].index("▰"), 10)
        self.assertEqual(narrow_lines[narrow_bar + 1], " " * 10 + "↑")
        self.assertNotIn("\u20dd", wide)
        self.assertNotIn("\u20dd", narrow)
        with mock.patch.object(noob.time, "time", return_value=now):
            colored = noob.render_home(usage, limits, width=80, color=True)
        self.assertIn("\033[1;38;2;46;139;87m" + " " * 17 + "↑\033[0m", colored)
        self.assertNotIn("\033[1;33m" + " " * 17 + "↑", colored)

    def test_model_totals_and_reference_estimate(self):
        buckets = [{"results": [{"model": "gpt-5.6-terra", "input_tokens": 1_000_000,
            "input_cached_tokens": 200_000, "input_cache_write_tokens": 100_000,
            "output_tokens": 100_000, "num_model_requests": 2}]},
            {"results": [{"model": "gpt-5.6-terra", "input_tokens": 1000,
            "output_tokens": 500, "num_model_requests": 1}]}]
        row = noob.model_totals(buckets)["gpt-5.6-terra"]
        self.assertEqual(row["requests"], 3)
        self.assertAlmostEqual(noob.estimate("gpt-5.6-terra", row), 2.898)
        self.assertIsNone(noob.estimate("unknown", row))

    def test_render_separates_actual_invoice_and_reference(self):
        api = (dt.datetime(2026, 9, 1), [{"results": [
            {"amount": {"currency": "usd", "value": 4.25}}]}], [{"results": [
            {"model": "gpt-5.6-luna", "input_tokens": 1000, "output_tokens": 200}]}])
        output = noob.render_home(noob.build_usage([]), {}, api)
        self.assertIn("Actual month-to-date: $4.25 USD", output)
        self.assertIn("reference is not", output)
        self.assertIn("the invoice", output)
        self.assertIn("PLATFORMS", output)

    def test_home_hides_model_table_and_detail_shows_it(self):
        usage = noob.build_usage([("Codex", {"files": 3})])
        home = noob.render_home(usage, {}, color=True)
        self.assertIn("PLATFORMS", home)
        self.assertNotIn("BY MODEL", home)
        self.assertIn("session files", home)
        detail = noob.render_models(usage, color=True)
        self.assertIn("MODELS", detail)
        self.assertNotIn("PLATFORMS", detail)
        self.assertNotIn("TOTAL", detail)

    def test_local_log_deltas_model_switch_and_month_boundary(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "sessions" / "2026" / "09" / "rollout-test.jsonl"
            path.parent.mkdir(parents=True)
            entries = [
                {"timestamp": "2026-08-31T23:00:00Z", "type": "turn_context",
                 "payload": {"model": "gpt-5.5"}},
                {"timestamp": "2026-08-31T23:01:00Z", "type": "event_msg",
                 "payload": {"type": "token_count", "info": {"total_token_usage": {
                     "input_tokens": 1000, "cached_input_tokens": 200, "output_tokens": 100}}}},
                {"timestamp": "2026-08-31T23:02:00Z", "type": "event_msg",
                 "payload": {"type": "token_count", "info": {"total_token_usage": {
                     "input_tokens": 1000, "cached_input_tokens": 200, "output_tokens": 100}}}},
                {"timestamp": "2026-09-22T09:00:00Z", "type": "turn_context",
                 "payload": {"model": "gpt-5.6-sol"}},
                {"timestamp": "2026-09-22T09:01:00Z", "type": "event_msg",
                 "payload": {"type": "token_count", "info": {"total_token_usage": {
                     "input_tokens": 2000, "cached_input_tokens": 500, "output_tokens": 200}}}},
            ]
            path.write_text("\n".join(json.dumps(entry) for entry in entries), encoding="utf-8")
            data = noob.scan_local_sessions(folder, dt.datetime(2026, 9, 22, 10, tzinfo=dt.timezone.utc))
        self.assertEqual(data["files"], 1)
        self.assertEqual(noob.local_summary(data["all"])[0], 2200)
        self.assertEqual(noob.local_summary(data["month"])[0], 1100)
        self.assertAlmostEqual(data["month"]["gpt-5.6-sol"]["estimate"], 0.00492)
        self.assertNotIn("gpt-5.5", data["month"])

    def test_auto_review_usage_is_excluded_without_leaking_into_next_model(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "sessions" / "rollout-review.jsonl"
            path.parent.mkdir(parents=True)
            entries = []
            for minute, model, total in ((0, "gpt-6-sol", 100),
                                         (1, "codex-auto-review", 150),
                                         (2, "gpt-6-sol", 200)):
                stamp = "2026-09-22T09:%02d:00Z" % minute
                entries.append({"timestamp": stamp, "type": "turn_context",
                                "payload": {"model": model}})
                entries.append({"timestamp": stamp, "type": "event_msg",
                                "payload": {"type": "token_count", "info": {
                                    "total_token_usage": {"input_tokens": total}}}})
            path.write_text("\n".join(json.dumps(entry) for entry in entries), encoding="utf-8")
            data = noob.scan_local_sessions(folder, dt.datetime(2026, 9, 22, 10, tzinfo=dt.timezone.utc))

        self.assertNotIn("codex-auto-review", data["all"])
        self.assertEqual(data["all"]["gpt-6-sol"]["input"], 150)
        self.assertEqual(noob.local_summary(data["all"])[0], 150)
        self.assertNotIn("codex-auto-review", noob.render_home(
            noob.build_usage([("Codex", data)]), {}, width=80))
        self.assertNotIn("codex-auto-review", noob.render_models(
            noob.build_usage([("Codex", data)]), width=80))
        card = ET.fromstring(noob_card.card_svg(noob.build_usage([("Codex", data)])))
        self.assertEqual(card.find(".//*[@id='models']").text, "1 ")

    def test_long_context_reference_multiplier(self):
        row = {"input": 300_000, "cached": 100_000, "write": 0, "output": 10_000}
        self.assertAlmostEqual(noob.estimate("gpt-6-astra", row, True), 4.95)

    def test_new_model_reference_rates_and_quota_colors(self):
        row = {"input": 1_000_000, "cached": 500_000, "write": 100_000,
               "output": 100_000}
        self.assertAlmostEqual(noob.estimate("gpt-6-sol", row), 2.15)
        self.assertEqual(noob.RATES["gpt-6-luna"], (0.1, 0.01, 0.125, 0.5))
        self.assertEqual([noob.quota_tone(value) for value in (86, 60, 59, 20, 19)],
                         ["32", "32", "33", "33", "31"])
        self.assertEqual(noob.bar(80, 10), "▰▰▰▰▰▰▰▰▱▱")
        self.assertEqual(noob.bar(0, 4), "▱▱▱▱")
        self.assertEqual(noob.bar(100, 4), "▰▰▰▰")
        self.assertIn("\033[31m▰", noob.bar(19, 10, color=True))

    def test_fixed_bar_keeps_terminal_column_width(self):
        self.assertEqual(len(noob.bar(100)), noob.QUOTA_BAR_CELLS)
        self.assertEqual(len(noob.bar(0)), noob.QUOTA_BAR_CELLS)
        for width in (8, 14, 17, 20):
            with self.subTest(width=width):
                result = noob.bar(58, width)
                columns = sum(2 if unicodedata.east_asian_width(char) in ("W", "F") else 1
                              for char in result)
                self.assertEqual(columns, width)

    def test_local_calendar_day_week_month_and_all_models(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "sessions" / "rollout-periods.jsonl"
            path.parent.mkdir(parents=True)
            now = dt.datetime.now().astimezone().replace(
                year=2026, month=9, day=23, hour=12, minute=0, second=0, microsecond=0)
            dates = ((8, 31), (9, 20), (9, 21), (9, 22), (9, 23))
            entries = [{"timestamp": now.isoformat(), "type": "turn_context",
                        "payload": {"model": "gpt-6-sol"}}]
            for index, (month, day) in enumerate(dates, 1):
                stamp = now.replace(month=month, day=day, hour=10)
                entries.append({"timestamp": stamp.isoformat(), "type": "event_msg",
                                "payload": {"type": "token_count", "info": {
                                    "total_token_usage": {"input_tokens": index * 100_000}}}})
            path.write_text("\n".join(json.dumps(entry) for entry in entries), encoding="utf-8")
            data = noob.scan_local_sessions(folder, now)
        self.assertEqual([data[period]["gpt-6-sol"]["input"]
                          for period in ("day", "week", "month", "all")],
                         [100_000, 300_000, 400_000, 500_000])
        self.assertEqual([round(data[period]["gpt-6-sol"]["estimate"], 2)
                          for period in ("day", "week", "month", "all")],
                         [0.2, 0.6, 0.8, 1.0])

    def test_compressed_rollout_sessions_keep_month_and_all_apart(self):
        try:
            from compression import zstd
        except ImportError:
            self.skipTest("this Python has no stdlib zstd support")
        now = dt.datetime(2026, 10, 29, 12, tzinfo=dt.timezone.utc)
        entries = [{"timestamp": "2026-09-22T09:00:00Z", "type": "turn_context",
                    "payload": {"model": "gpt-6-sol"}},
                   {"timestamp": "2026-09-22T09:01:00Z", "type": "event_msg",
                    "payload": {"type": "token_count", "info": {
                        "total_token_usage": {"input_tokens": 100_000}}}}]
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "sessions" / "2026" / "09" / "rollout-september.jsonl"
            path.parent.mkdir(parents=True)
            path.write_text("\n".join(json.dumps(entry) for entry in entries), encoding="utf-8")
            path.with_suffix(".jsonl.zst").write_bytes(zstd.compress(path.read_bytes()))
            path.unlink()  # Codex leaves only the compressed archive once a rollout ages out
            data = noob.scan_local_sessions(folder, now)
        self.assertEqual(data["files"], 1)
        self.assertEqual(noob.local_summary(data["all"])[0], 100_000)
        self.assertEqual(noob.local_summary(data["month"])[0], 0)
        self.assertAlmostEqual(data["all"]["gpt-6-sol"]["estimate"], 0.2)
        self.assertEqual(data["month"], {})

    def test_rollout_seen_in_both_forms_is_counted_once(self):
        try:
            from compression import zstd
        except ImportError:
            self.skipTest("this Python has no stdlib zstd support")
        now = dt.datetime(2026, 10, 29, 12, tzinfo=dt.timezone.utc)
        entries = [{"timestamp": "2026-10-28T09:00:00Z", "type": "turn_context",
                    "payload": {"model": "gpt-6-sol"}},
                   {"timestamp": "2026-10-28T09:01:00Z", "type": "event_msg",
                    "payload": {"type": "token_count", "info": {
                        "total_token_usage": {"input_tokens": 500_000}}}}]
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "sessions" / "rollout-half-written.jsonl"
            path.parent.mkdir(parents=True)
            text = "\n".join(json.dumps(entry) for entry in entries)
            path.write_text(text, encoding="utf-8")
            path.with_suffix(".jsonl.zst").write_bytes(zstd.compress(text.encode("utf-8")))
            data = noob.scan_local_sessions(folder, now)
        self.assertEqual(data["files"], 1)
        self.assertEqual(noob.local_summary(data["all"])[0], 500_000)

    def test_compressed_sessions_fall_back_to_the_zstd_command(self):
        try:
            from compression import zstd
        except ImportError:
            self.skipTest("this Python has no stdlib zstd support")
        if not shutil.which("zstd"):
            self.skipTest("the zstd command is not installed")
        now = dt.datetime(2026, 10, 29, 12, tzinfo=dt.timezone.utc)
        entries = [{"timestamp": "2026-10-28T09:00:00Z", "type": "turn_context",
                    "payload": {"model": "gpt-6-sol"}},
                   {"timestamp": "2026-10-28T09:01:00Z", "type": "event_msg",
                    "payload": {"type": "token_count", "info": {
                        "total_token_usage": {"input_tokens": 250_000}}}}]
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "sessions" / "rollout-cli.jsonl.zst"
            path.parent.mkdir(parents=True)
            path.write_bytes(zstd.compress("\n".join(json.dumps(entry) for entry in entries)
                                           .encode("utf-8")))
            with mock.patch.dict(sys.modules, {"compression": None, "zstandard": None}):
                data = noob.scan_local_sessions(folder, now)
        self.assertEqual(data["files"], 1)
        self.assertEqual(noob.local_summary(data["all"])[0], 250_000)

    def test_model_columns_default_today_and_click_sort(self):
        def row(value):
            return {"input": 100_000, "cached": 0, "write": 0, "output": 0,
                    "estimate": value, "unpriced": 0, "calls": 1}
        source = {"day": {"gpt-6-sol": row(1), "gpt-5.5": row(2)},
                  "week": {"gpt-6-sol": row(3), "gpt-5.5": row(4)},
                  "month": {"gpt-6-sol": row(5), "gpt-5.5": row(4)},
                  "all": {"gpt-6-sol": row(7), "gpt-5.5": row(6)}}
        usage = noob.build_usage([("Codex", source)])
        default = noob.render_models(usage, width=80)
        self.assertIn("gpt-6-sol", default)
        self.assertIn("T/W/M/A: sort", default)
        self.assertIn("TODAY ↓", default)
        self.assertLess(default.index("gpt-5.5"), default.index("gpt-6-sol"))
        self.assertEqual(noob.SORT_KEYS, {"t": "day", "w": "week", "m": "month", "a": "all"})
        period, descending = noob.next_sort("day", True, noob.SORT_KEYS["t"])
        self.assertEqual((period, descending), ("day", False))
        ascending_day = noob.render_models(usage, width=80, sort_period=period, sort_desc=descending)
        self.assertIn("TODAY ↑", ascending_day)
        self.assertLess(ascending_day.index("gpt-6-sol"), ascending_day.index("gpt-5.5"))
        period, descending = noob.next_sort(period, descending, noob.SORT_KEYS["m"])
        self.assertEqual((period, descending), ("month", True))
        sorted_month = noob.render_models(usage, width=80, sort_period=period, sort_desc=descending)
        self.assertIn("MONTH ↓", sorted_month)
        self.assertLess(sorted_month.index("gpt-6-sol"), sorted_month.index("gpt-5.5"))

    def test_mouse_reports_are_swallowed_without_shortcuts(self):
        # The models view sorts with keys now, so a click must not leak its
        # trailing letter (the "M" in a mouse report) into the shortcut handler.
        read_fd, write_fd = os.pipe()
        try:
            os.write(write_fd, b"\x1b[<0;26;17M\x1b[<0;26;17m")
            keys = []
            with mock.patch.object(noob.sys, "stdout", io.StringIO()):
                noob.wait_for_refresh(time.monotonic() + 0.02, 80, False,
                                      input_fd=read_fd, on_key=keys.append)
            self.assertEqual(keys, [])
        finally:
            os.close(read_fd)
            os.close(write_fd)

    def test_terminal_sequences_and_shortcuts_survive_split_reads(self):
        incoming = (b"s\x1b[<0;26;17M\x1b[<0;26;17m"
                    b"\x1b[<64;26;17M\x1b[<65;26;17M"  # wheel
                    b"\x1b[A\x1bOS\x1b[1;2S\x1b[0m\x1bmMmsS")
        expected = [("key", ord("s"))]
        expected.extend(("key", key) for key in b"MmsS")
        # Split at every boundary, including immediately before mouse M/m.
        for boundary in range(len(incoming) + 1):
            with self.subTest(boundary=boundary):
                parser = noob.TerminalInput()
                events = parser.feed(incoming[:boundary])
                events.extend(parser.feed(incoming[boundary:]))
                self.assertEqual(events, expected)
        parser = noob.TerminalInput()
        self.assertEqual([event for key in incoming for event in parser.feed(bytes([key]))],
                         expected)

    def test_split_mouse_event_across_refreshes_does_not_fire_shortcuts(self):
        read_fd, write_fd = os.pipe()
        parser = noob.TerminalInput()
        keys = []
        try:
            with mock.patch.object(noob.sys, "stdout", io.StringIO()):
                for chunk in (b"\x1b[<0;26;17", b"M\x1b[<0;26;17", b"m", b"mS"):
                    os.write(write_fd, chunk)
                    noob.wait_for_refresh(
                        time.monotonic() + 0.02, 80, False, input_fd=read_fd,
                        input_parser=parser, on_key=keys.append)
            self.assertEqual(keys, ["m", "S"])
        finally:
            os.close(read_fd)
            os.close(write_fd)

    def test_s_key_saves_current_usage_and_shows_green_path(self):
        class Terminal(io.StringIO):
            def isatty(self):
                return True

        terminal = Terminal()
        local = {period: {} for period in ("day", "week", "month", "all")}
        local["files"] = 2
        limits = {"rateLimits": {"limitId": "codex", "primary": {
            "usedPercent": 70, "windowDurationMins": 300}, "secondary": {
            "usedPercent": 91, "windowDurationMins": 10080}}}
        fresh_limits = {"rateLimits": {"limitId": "codex", "primary": {
            "usedPercent": 20, "windowDurationMins": 300}, "secondary": {
            "usedPercent": 61, "windowDurationMins": 10080}}}
        saved = Path("/Users/eke/Downloads/noobsolo-usage-test.png")

        def save_once(*args, **kwargs):
            kwargs["on_key"]("s")
            raise KeyboardInterrupt

        with mock.patch.object(noob.sys, "stdout", terminal), \
             mock.patch.object(noob, "codex_data", side_effect=[limits, fresh_limits]) as quota, \
             mock.patch.object(noob, "scan_local_sessions", return_value=local) as scan, \
             mock.patch.object(noob, "save_card", return_value=saved) as card, \
             mock.patch.object(noob, "wait_for_refresh", side_effect=save_once):
            self.assertEqual(noob.main([]), 0)
        self.assertEqual(quota.call_count, 2)
        self.assertEqual(scan.call_count, 2)
        saved = card.call_args.args[0]
        self.assertEqual(saved["files"], 2)
        self.assertEqual(card.call_args.kwargs["quotas"], {300: (80, "32"), 10080: (39, "33")})
        self.assertIsNone(card.call_args.kwargs["deepseek"])
        self.assertIn("Saved image: /Users/eke/Downloads/noobsolo-usage-test.png",
                      terminal.getvalue())
        self.assertIn("\033[1;92m", terminal.getvalue())

        read_fd, write_fd = os.pipe()
        try:
            os.write(write_fd, b"sS")
            saves = []
            with mock.patch.object(noob.sys, "stdout", io.StringIO()):
                noob.wait_for_refresh(time.monotonic() + 0.02, 80, False,
                                      input_fd=read_fd, on_key=lambda char: saves.append(char))
            self.assertEqual(saves, ["s", "S"])
        finally:
            os.close(read_fd)
            os.close(write_fd)

    def test_m_key_saves_fresh_reset_credits_and_shows_path(self):
        class Terminal(io.StringIO):
            def isatty(self):
                return True

        terminal = Terminal()
        initial = {"rateLimitResetCredits": {"availableCount": 0, "credits": []}}
        fresh = {"rateLimitResetCredits": {"availableCount": 2, "credits": [
            {"status": "available", "expiresAt": 1791622800},
            {"status": "available", "expiresAt": 1791901800}]}}
        saved = Path("/Users/eke/Downloads/noobsolo-reset-test.png")

        def save_once(*args, **kwargs):
            kwargs["on_key"]("m")
            raise KeyboardInterrupt

        with mock.patch.object(noob.sys, "stdout", terminal), \
             mock.patch.object(noob, "codex_data", side_effect=[initial, fresh]) as quota, \
             mock.patch.object(noob, "scan_local_sessions", return_value={}) as scan, \
             mock.patch.object(noob, "save_reset_card", return_value=saved) as card, \
             mock.patch.object(noob, "wait_for_refresh", side_effect=save_once):
            self.assertEqual(noob.main([]), 0)
        self.assertEqual(quota.call_count, 2)
        scan.assert_called_once()
        card.assert_called_once_with(fresh["rateLimitResetCredits"])
        self.assertIn("Saved reset card: /Users/eke/Downloads/noobsolo-reset-test.png",
                      terminal.getvalue())
        self.assertIn("\033[1;92m", terminal.getvalue())

        read_fd, write_fd = os.pipe()
        try:
            os.write(write_fd, b"mM")
            saves = []
            with mock.patch.object(noob.sys, "stdout", io.StringIO()):
                noob.wait_for_refresh(time.monotonic() + 0.02, 80, False,
                                      input_fd=read_fd, on_key=lambda char: saves.append(char))
            self.assertEqual(saves, ["m", "M"])
        finally:
            os.close(read_fd)
            os.close(write_fd)

    def test_reset_card_shows_sorted_available_expiries(self):
        zone = dt.datetime.now().astimezone().tzinfo
        now = dt.datetime(2026, 10, 9, 15, 30, tzinfo=zone)
        dates = [dt.datetime(2026, 10, day, hour, minute, tzinfo=zone).timestamp()
                 for day, hour, minute in ((18, 9, 0), (10, 9, 0), (13, 18, 30))]
        credits = {"availableCount": 3, "credits": [
            {"status": "available", "expiresAt": dates[0]},
            {"status": "used", "expiresAt": dates[0]},
            {"status": "available", "expiresAt": dates[1]},
            {"status": "available", "expiresAt": dates[2]}]}
        root = ET.fromstring(noob_reset_card.reset_card_svg(credits, now))
        node = lambda node_id: root.find(".//*[@id='%s']" % node_id)
        self.assertEqual(node("available-count").text, "03")
        self.assertEqual(node("card-date").text, "RESET CARD / 09 OCT 2026")
        self.assertEqual(node("card-status").text, "ACCOUNT SNAPSHOT · 15:30")
        self.assertEqual(root.find(".//*[@fill='#b00065']").get("y"), "170")
        self.assertEqual(node("expiry-rows")[0].get("x1"), "137")
        self.assertEqual(node("expiry-rows")[0].get("y1"), "647")
        self.assertEqual(node("expiry-rows")[0].get("y2"), "1057")
        texts = [item.text for item in node("expiry-rows") if item.tag.endswith("text")]
        self.assertEqual(texts, ["10 OCT", "09:00", "RESET CREDIT  01",
                                 "13 OCT", "18:30", "RESET CREDIT  02",
                                 "18 OCT", "09:00", "RESET CREDIT  03"])
        self.assertNotIn("SAMPLE DATA", ET.tostring(root, encoding="unicode"))

    def test_reset_card_handles_zero_and_missing_expiries(self):
        zero = ET.fromstring(noob_reset_card.reset_card_svg(
            {"availableCount": 0, "credits": []}))
        self.assertEqual(zero.find(".//*[@id='available-count']").text, "00")
        self.assertIn("NO RESET CREDITS AVAILABLE", ET.tostring(zero, encoding="unicode"))
        unknown = ET.fromstring(noob_reset_card.reset_card_svg(
            {"availableCount": 1, "credits": [{"status": "available"}]}))
        self.assertIn("DATE UNKNOWN", ET.tostring(unknown, encoding="unicode"))
        for bad in (None, {}, {"availableCount": True},
                    {"availableCount": 0, "credits": [{"status": "available"}]}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                noob_reset_card.reset_card_svg(bad)

    def test_card_uses_local_totals_and_random_avatar(self):
        def row(tokens, amount):
            return {"input": tokens, "output": 0, "estimate": amount, "unpriced": 0}

        source = {"day": {"gpt-6-sol": row(100, 1.25)},
                  "week": {"gpt-6-sol": row(200, 2.5)},
                  "month": {"gpt-6-sol": row(300, 3.75),
                            "gpt-6-luna": row(50, 0.5)},
                  "all": {"gpt-6-sol": row(500, 6.25)}, "files": 7}
        usage = noob.build_usage([("Codex", source)])
        now = dt.datetime(2026, 9, 23, 17, 30)
        first = noob_card.card_svg(usage, now, random.Random(1))
        second = noob_card.card_svg(usage, now, random.Random(2))
        root = ET.fromstring(first)

        def node(node_id):
            return root.find(".//*[@id='%s']" % node_id)

        self.assertEqual(node("card-date").text, "23 SEP 2026")
        self.assertEqual(node("tokens").text, "500 ")
        self.assertEqual(node("sessions").text, "7 ")
        self.assertEqual(node("models").text, "2 ")
        self.assertEqual(node("today-amount").text + node("today-amount")[0].text, "$1.25")
        self.assertEqual(node("month-amount").text + node("month-amount")[0].text, "$4.25")
        rows_node = node("platform-rows")
        texts = rows_node.findall("{%s}text" % noob_card.SVG_NS)
        self.assertEqual([t.text for t in texts], ["CODEX", "$1.25", "$2.50", "$4.25"])
        self.assertNotIn("SAMPLE DATA", first)
        self.assertNotEqual(ET.tostring(node("avatar-art")),
                            ET.tostring(ET.fromstring(second).find(".//*[@id='avatar-art']")))

    def test_card_deepseek_split_renders_sub_rows(self):
        codex = {"day": {"gpt-6-sol": {"input": 100, "output": 0, "estimate": 1.25}},
                 "week": {"gpt-6-sol": {"input": 200, "output": 0, "estimate": 2.5}},
                 "month": {"gpt-6-sol": {"input": 300, "output": 0, "estimate": 3.75}},
                 "all": {"gpt-6-sol": {"input": 500, "output": 0, "estimate": 6.25}}}
        deepseek = {"day": {"DeepSeek-V4.1-Flash": {"input": 1, "output": 1, "estimate": 0.1, "estimate_cny": 0.5}},
                    "week": {"DeepSeek-V4.1-Flash": {"input": 1, "output": 1, "estimate": 0.2, "estimate_cny": 1.0}},
                    "month": {"DeepSeek-V4.1-Flash": {"input": 1, "output": 1, "estimate": 0.3, "estimate_cny": 1.5}},
                    "all": {"DeepSeek-V4.1-Flash": {"input": 1, "output": 1, "estimate": 0.4, "estimate_cny": 2.0}}}
        usage = noob.build_usage([("Codex", codex), ("DeepSeek", deepseek)])
        usage["deepseek_split"] = {
            "opencode": {"cny": {"day": 0.4, "week": 0.9, "month": 1.4, "all": 1.9}},
            "pi": {"cny": {"day": 0.1, "week": 0.1, "month": 0.1, "all": 0.1}}}
        root = ET.fromstring(noob_card.card_svg(usage))
        rows_node = root.find(".//*[@id='platform-rows']")
        texts = rows_node.findall("{%s}text" % noob_card.SVG_NS)
        # Four rows (CODEX, DEEPSEEK, opencode, pi), four cells each.
        self.assertEqual(len(texts), 16)
        self.assertEqual([texts[i].text for i in (0, 4, 8, 12)],
                         ["CODEX", "DEEPSEEK", "opencode", "pi"])
        self.assertEqual([texts[9].text, texts[10].text, texts[11].text],
                         ["¥0.40", "¥0.90", "¥1.40"])
        self.assertEqual([texts[13].text, texts[14].text, texts[15].text],
                         ["¥0.10", "¥0.10", "¥0.10"])
        # Sub-row names are indented and dimmer than the main rows.
        self.assertEqual(rows_node[8].get("x"), "106")
        self.assertEqual(rows_node[8].get("fill"), "#5b6670")
        self.assertEqual(rows_node[0].get("x"), "88")

    def test_card_quota_bars_show_remaining_and_terminal_color(self):
        usage = noob.build_usage([])
        root = ET.fromstring(noob_card.card_svg(usage, quotas={
            300: (75, "32"), 10080: (35, "33")}))
        node = lambda node_id: root.find(".//*[@id='%s']" % node_id)
        self.assertEqual(float(node("quota-5h-fill").get("width")), round(161 * .75, 2))
        self.assertEqual(float(node("quota-week-fill").get("width")), round(161 * .35, 2))
        self.assertEqual(node("quota-5h-fill").get("fill"), noob_card.QUOTA_COLORS["32"])
        self.assertEqual(node("quota-week-fill").get("fill"), noob_card.QUOTA_COLORS["33"])
        self.assertIsNone(node("quota-5h-unavailable").text)
        self.assertNotIn("go/noobsolo", ET.tostring(root, encoding="unicode"))

        red = ET.fromstring(noob_card.card_svg(usage, quotas={300: (10, "31")}))
        self.assertEqual(red.find(".//*[@id='quota-5h-fill']").get("fill"),
                         noob_card.QUOTA_COLORS["31"])
        self.assertEqual(float(red.find(".//*[@id='quota-5h-fill']").get("width")),
                         round(161 * .10, 2))

        missing = ET.fromstring(noob_card.card_svg(usage))
        self.assertEqual(missing.find(".//*[@id='quota-5h-fill']").get("width"), "0")
        self.assertEqual(missing.find(".//*[@id='quota-week-unavailable']").text, "—")

    def test_api_pagination_and_admin_auth(self):
        pages = [
            {"data": [{"results": [{"amount": {"currency": "usd", "value": 1.0}}]}],
             "has_more": True, "next_page": "next"},
            {"data": [{"results": [{"amount": {"currency": "usd", "value": 2.0}}]}],
             "has_more": False, "next_page": None},
        ]
        requests = []

        def fake_open(request, timeout):
            requests.append(request)
            response = mock.MagicMock()
            response.__enter__.return_value = io.BytesIO(json.dumps(pages.pop(0)).encode())
            return response

        with mock.patch.object(noob.urllib.request, "urlopen", side_effect=fake_open):
            buckets = list(noob.api_pages("/organization/costs", "secret", {"start_time": 1}))
        self.assertEqual(len(buckets), 2)
        self.assertEqual(requests[0].get_header("Authorization"), "Bearer secret")
        self.assertIn("page=next", requests[1].full_url)

    def test_interactive_mode_refreshes_and_ctrl_c_exits(self):
        class Terminal(io.StringIO):
            def isatty(self):
                return True

        terminal = Terminal()
        local = {"all": {}, "month": {}, "files": 0}
        snapshots = iter((
            {"rateLimitResetCredits": {"availableCount": 1, "credits": [
                {"status": "available", "expiresAt": 1792697605}]}},
            {"rateLimitResetCredits": {"availableCount": 0, "credits": []}},
        ))
        def quota():
            self.assertIn("Loading...", terminal.getvalue())
            return next(snapshots)

        with mock.patch.object(noob.sys, "stdout", terminal), \
             mock.patch.object(noob, "codex_data", side_effect=quota) as codex, \
             mock.patch.object(noob, "scan_local_sessions", return_value=local), \
             mock.patch.object(noob, "wait_for_refresh", side_effect=[None, KeyboardInterrupt]) as wait:
            result = noob.main([])
        self.assertEqual(result, 0)
        self.assertEqual(codex.call_count, 2)
        self.price_refresh.assert_called_once_with()
        self.assertEqual(terminal.getvalue().count("Loading..."), 1)
        self.assertEqual(terminal.getvalue().count("Refresh in "), 2)
        self.assertEqual(wait.call_count, 2)
        self.assertEqual(terminal.getvalue().count("CODEX RESET  ·  1 AVAILABLE"), 2)
        self.assertNotIn("CODEX RESET  ·  0 AVAILABLE", terminal.getvalue())

    def test_countdown_updates_only_the_footer(self):
        class Terminal(io.StringIO):
            def isatty(self):
                return True

        terminal = Terminal()
        clock = [100.0]

        def sleep(seconds):
            clock[0] += seconds

        with mock.patch.object(noob.sys, "stdout", terminal), \
             mock.patch.object(noob.time, "monotonic", side_effect=lambda: clock[0]), \
             mock.patch.object(noob.time, "sleep", side_effect=sleep):
            noob.wait_for_refresh(103.0, 80, False)

        self.assertEqual(terminal.getvalue(),
                         "".join("\033[1A\r\033[2K%s\n" % noob.countdown_line(seconds, 80, False)
                                 for seconds in (2, 1)))
        self.assertEqual(clock[0], 103.0)

    def test_version_badge_sits_in_the_bottom_right(self):
        usage = noob.build_usage([])
        for width in (20, 40, 80):
            with self.subTest(width=width):
                plain = noob.render_home(usage, {}, width=width, watch=True, refresh_in=58)
                footer = plain.splitlines()[-1]
                self.assertEqual(len(footer), width)
                self.assertTrue(footer.endswith("v" + noob.VERSION))
                once = noob.render_home(usage, {}, width=width)
                self.assertEqual(len(once.splitlines()[-1]), width)
                self.assertTrue(once.splitlines()[-1].endswith("v" + noob.VERSION))
        colored = noob.render_home(usage, {}, width=80, watch=True, refresh_in=58, color=True)
        self.assertIn("\033[2mv%s\033[0m" % noob.VERSION, colored.splitlines()[-1])
        updated = noob.render_home(usage, {}, width=80, watch=True, refresh_in=58,
                                   color=True, new_version=True)
        self.assertIn("\033[1;92mv%s\033[0m" % noob.VERSION, updated.splitlines()[-1])

    def test_installed_update_greens_the_version_badge_instead_of_a_notice(self):
        class Terminal(io.StringIO):
            def isatty(self):
                return True

        terminal = Terminal()
        notice = "noob updated: 1111111 -> 2222222 (2 new commits)"
        with mock.patch.dict(os.environ, {noob.UPDATE_NOTICE_ENV: notice}), \
             mock.patch.object(noob.sys, "stdout", terminal), \
             mock.patch.object(noob, "codex_data", return_value={}), \
             mock.patch.object(noob, "scan_local_sessions", return_value={}), \
             mock.patch.object(noob, "wait_for_refresh", side_effect=KeyboardInterrupt) as wait:
            self.assertEqual(noob.main([]), 0)
        output = terminal.getvalue()
        self.assertNotIn("noob updated", output)
        self.assertNotIn("new commits", output)
        self.assertIn("\033[1;92mv%s\033[0m" % noob.VERSION, output)
        self.assertEqual(wait.call_args.kwargs["new_version"], True)
        self.assertNotIn(noob.UPDATE_NOTICE_ENV, os.environ)

    def test_ctrl_c_during_loading_exits_cleanly(self):
        class Terminal(io.StringIO):
            def isatty(self):
                return True

        terminal = Terminal()
        with mock.patch.object(noob.sys, "stdout", terminal), \
             mock.patch.object(noob, "codex_data", side_effect=KeyboardInterrupt):
            result = noob.main([])
        self.assertEqual(result, 0)
        self.assertIn("Loading...", terminal.getvalue())
        self.assertNotIn("PLATFORMS", terminal.getvalue())

    def test_layout_fits_terminal_widths(self):
        limits = {"rateLimits": {"limitId": "codex", "primary": {
            "usedPercent": 22, "windowDurationMins": 300, "resetsAt": 1789999999}}}
        row = {"input": 6_025_396, "cached": 5_767_680, "write": 0,
               "output": 26_535, "estimate": 3.87, "unpriced": 0, "calls": 10}
        source = {"day": {"gpt-5.6-sol": row}, "week": {"gpt-5.6-sol": row},
                  "month": {"gpt-5.6-sol": row}, "all": {"gpt-5.6-sol": row}, "files": 7}
        usage = noob.build_usage([("Codex", source),
                                  ("DeepSeek", {"day": {"deepseek-flash": row}})])
        api = (dt.datetime(2026, 9, 1), [{"results": [
            {"amount": {"currency": "usd", "value": 4.25}}]}], [{"results": [
            {"model": "gpt-5.6-luna", "input_tokens": 1000, "output_tokens": 200}]}])
        balance = [{"currency": "CNY", "total": 110.0, "granted": 10.0, "topped_up": 100.0}]
        for width in (20, 32, 40, 52, 64, 80):
            with self.subTest(width=width):
                for output in (noob.render_home(usage, limits, width=width, watch=True, color=True,
                                                reset_credits={"availableCount": 1, "credits": [
                                                    {"status": "available", "expiresAt": 1792697605}]},
                                                deepseek=balance),
                               noob.render_models(usage, width=width, watch=True, color=True),
                               noob.render_home(noob.build_usage([]), {}, api, width=width,
                                                warnings=("Temporary network issue",)),
                               noob.render_loading(width=width, color=True)):
                    plain = re.sub(r"\x1b\[[0-9;]*m", "", output)
                    self.assertFalse(re.search(r"[\u4e00-\u9fff]", plain))
                    self.assertLessEqual(max(map(len, plain.splitlines())), width)
                    if "PLATFORMS" in plain:
                        self.assertTrue(plain.splitlines()[0].startswith("  ★"))

    def test_reset_banner_shows_each_available_expiry(self):
        times = [dt.datetime(2026, 10, day, hour, minute).timestamp()
                 for day, hour, minute in ((18, 9, 0), (10, 9, 0), (13, 18, 30))]
        credits = {"availableCount": 3, "credits": [
            {"status": "available", "expiresAt": times[0]},
            {"status": "used", "expiresAt": times[0]},
            {"status": "available", "expiresAt": times[1]},
            {"status": "available", "expiresAt": times[2]}]}
        wide = noob.reset_banner(credits, 80).splitlines()
        self.assertEqual(len(wide), 2)
        self.assertIn("CODEX RESET  ·  3 AVAILABLE", wide[0])
        self.assertIn("EXPIRES", wide[0])
        self.assertLess(wide[1].index("① Oct 10 09:00"), wide[1].index("② Oct 13 18:30"))
        self.assertLess(wide[1].index("② Oct 13 18:30"), wide[1].index("③ Oct 18 09:00"))
        self.assertTrue(all(len(line) == 80 for line in wide))
        narrow = noob.reset_banner(credits, 52).splitlines()
        self.assertEqual(len(narrow), 5)
        self.assertEqual([line.strip() for line in narrow[2:]],
                         ["① Oct 10 09:00", "② Oct 13 18:30", "③ Oct 18 09:00"])
        self.assertIn("RESETS: 3", noob.reset_banner(credits, 20))
        self.assertIn("CODEX RESET  ·  0 AVAILABLE", noob.reset_banner({"availableCount": 0}, 80))
        self.assertIn("CODEX RESET  ·  UNKNOWN", noob.reset_banner(None, 80))
        many = {"availableCount": 6, "credits": [
            {"status": "available", "expiresAt": times[index % len(times)]}
            for index in range(6)]}
        many_lines = noob.reset_banner(many, 80).splitlines()
        self.assertGreater(len(many_lines), 2)
        self.assertIn("⑥ Oct 18 09:00", "\n".join(many_lines))
        self.assertTrue(all(len(line) == 80 for line in many_lines))

        source = {"day": {"m": {"input": 1, "cached": 0, "write": 0, "output": 0,
                                "estimate": 1.0, "unpriced": 0, "calls": 1}}}
        output = noob.render_models(noob.build_usage([("Codex", source)]), width=80)
        self.assertIn("MODEL", output.splitlines()[3])

    def test_subscription_text_appears_in_the_codex_section(self):
        info = {"renewal_date": "2026-10-22", "started_on": "2026-09-22"}
        today = dt.date(2026, 10, 9)
        self.assertEqual(noob.subscription_text(info, 80, today=today),
                         "oct 22, 2026  ·  subscribed 17 days")
        for available in (20, 32, 52, 80):
            with self.subTest(available=available):
                self.assertLessEqual(len(noob.subscription_text(info, available, today=today)),
                                     available)
        self.assertIn("subscribed — days", noob.subscription_text(
            {"renewal_date": "2026-10-22", "started_on": None}, 80, today=today))
        self.assertIn("unverified", noob.subscription_text(
            info, 80, today=dt.date(2026, 10, 23)))
        self.assertIn("subscribed 18 days", noob.subscription_text(
            info, 80, today=dt.date(2026, 10, 10)))

        credits = {"availableCount": 1, "credits": [
            {"status": "available", "expiresAt": dt.datetime(2026, 10, 23).timestamp()}]}
        with mock.patch.object(noob.dt, "date", wraps=dt.date) as date_type:
            date_type.today.return_value = today
            output = noob.render_home(noob.build_usage([]), {}, width=80,
                                      reset_credits=credits, subscription_info=info)
        lines = output.splitlines()
        codex = next(i for i, line in enumerate(lines) if "CODEX" in line and "RESET" not in line)
        renewal = next(i for i, line in enumerate(lines) if "renews" in line)
        self.assertLess(codex, renewal)
        self.assertIn("oct 22, 2026  ·  subscribed 17 days", lines[renewal])
        source = {"day": {"m": {"input": 1, "cached": 0, "write": 0, "output": 0,
                                "estimate": 1.0, "unpriced": 0, "calls": 1}}}
        detail = noob.render_models(noob.build_usage([("Codex", source)]), width=80)
        self.assertIn("MODEL", detail.splitlines()[3])


    def test_deepseek_balance_parses_per_currency_entries(self):
        payload = {"is_available": True, "balance_infos": [
            {"currency": "CNY", "total_balance": "110.00",
             "granted_balance": "10.00", "topped_up_balance": "100.00"},
            {"currency": "USD", "total_balance": "3.5",
             "granted_balance": "0", "topped_up_balance": "3.5"}]}
        requests = []

        def fake_open(request, timeout):
            requests.append(request)
            response = mock.MagicMock()
            response.__enter__.return_value = io.BytesIO(json.dumps(payload).encode())
            return response

        with mock.patch.object(noob.urllib.request, "urlopen", side_effect=fake_open):
            balance = noob.deepseek_balance("sk-test")
        self.assertEqual([entry["currency"] for entry in balance], ["CNY", "USD"])
        self.assertAlmostEqual(balance[0]["total"], 110.0)
        self.assertAlmostEqual(balance[0]["granted"], 10.0)
        self.assertAlmostEqual(balance[1]["topped_up"], 3.5)
        self.assertEqual(requests[0].get_header("Authorization"), "Bearer sk-test")
        self.assertIn("/user/balance", requests[0].full_url)
        # Everything is reported in CNY; USD entries are converted at the rate.
        self.assertEqual(noob.deepseek_money_text(balance, 5.0), "¥127.50")
        self.assertEqual(noob.deepseek_money_text(
            [{"currency": "USD", "total": 3.5}], 5.0), "¥17.50")
        self.assertIsNone(noob.deepseek_money_text(None))

    def test_deepseek_balance_reports_key_and_payload_errors(self):
        def http_error(code):
            return noob.urllib.error.HTTPError(noob.DEEPSEEK_BALANCE_URL, code, "err", {}, None)

        for code in (401, 403):
            with self.subTest(code=code), \
                 mock.patch.object(noob.urllib.request, "urlopen", side_effect=http_error(code)), \
                 self.assertRaises(RuntimeError):
                noob.deepseek_balance("bad-key")

        def body(payload):
            def fake_open(request, timeout):
                response = mock.MagicMock()
                response.__enter__.return_value = io.BytesIO(json.dumps(payload).encode())
                return response
            return fake_open

        for payload in ({"balance_infos": [{"currency": "CNY", "total_balance": "oops"}]},
                        {"balance_infos": [{"currency": "CNY"}]},
                        {"balance_infos": "not-a-list"}, {}):
            with self.subTest(payload=payload), \
                 mock.patch.object(noob.urllib.request, "urlopen", side_effect=body(payload)), \
                 self.assertRaises(RuntimeError):
                noob.deepseek_balance("key")

    def test_fetch_usd_cny_reads_rate_and_returns_none_on_failure(self):
        def response(payload):
            value = mock.MagicMock()
            value.__enter__.return_value = io.BytesIO(json.dumps(payload).encode())
            return value

        with mock.patch.object(noob.urllib.request, "urlopen",
                               return_value=response({"rates": {"CNY": 7.05}})) as opened:
            self.assertAlmostEqual(self.real_fetch_usd_cny(), 7.05)
        self.assertIn("/latest", opened.call_args.args[0].full_url)
        for bad in ({"rates": {}}, {"rates": {"CNY": "x"}}, {"rates": {"CNY": -1}}):
            with self.subTest(payload=bad), \
                 mock.patch.object(noob.urllib.request, "urlopen", return_value=response(bad)):
                self.assertIsNone(self.real_fetch_usd_cny())
        with mock.patch.object(noob.urllib.request, "urlopen", side_effect=OSError("offline")):
            self.assertIsNone(self.real_fetch_usd_cny())

    def test_fetch_deepseek_models_reads_names(self):
        payload = {"data": [{"id": "deepseek-flash", "name": "DeepSeek-V4.1-Flash"}]}
        value = mock.MagicMock()
        value.__enter__.return_value = io.BytesIO(json.dumps(payload).encode())
        with mock.patch.object(noob.urllib.request, "urlopen", return_value=value):
            self.assertEqual(self.real_fetch_deepseek_models("sk-test"),
                             {"deepseek-flash": "DeepSeek-V4.1-Flash"})
        self.assertIsNone(self.real_fetch_deepseek_models(None))
        with mock.patch.object(noob.urllib.request, "urlopen", side_effect=OSError("offline")):
            self.assertIsNone(self.real_fetch_deepseek_models("sk-test"))

    def test_deepseek_models_history_round_trips(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "deepseek_models.json"
            with mock.patch.object(noob, "DEEPSEEK_MODELS_PATH", path):
                self.assertEqual(self.real_load_deepseek_models(), {})
                history = {"deepseek-flash": [{"name": "DeepSeek-V4.1-Flash",
                                               "since": "2026-10-01T00:00:00+00:00"}]}
                self.real_save_deepseek_models(history)
                self.assertEqual(self.real_load_deepseek_models(), history)
                path.write_text("broken")
                self.assertEqual(self.real_load_deepseek_models(), {})

    def test_record_and_span_deepseek_versions(self):
        t0 = dt.datetime(2026, 10, 1, 12, 0, tzinfo=dt.timezone.utc)
        history = {}
        self.assertTrue(noob.record_deepseek_versions(history, {"deepseek-flash": "V4.1"}, t0))
        self.assertFalse(noob.record_deepseek_versions(
            history, {"deepseek-flash": "V4.1"}, t0 + dt.timedelta(days=1)))
        self.assertTrue(noob.record_deepseek_versions(
            history, {"deepseek-flash": "V4.5"}, t0 + dt.timedelta(days=2)))
        self.assertEqual([entry["name"] for entry in history["deepseek-flash"]], ["V4.1", "V4.5"])
        self.assertEqual(noob.deepseek_version_at(history, "deepseek-flash", t0), "V4.1")
        self.assertEqual(noob.deepseek_version_at(
            history, "deepseek-flash", t0 + dt.timedelta(days=3)), "V4.5")
        spans = noob.deepseek_version_spans(
            history, "deepseek-flash", t0, t0 + dt.timedelta(days=3))
        self.assertEqual([name for name, _, _ in spans], ["V4.1", "V4.5"])
        # A lone version record must not split the range into two same-name spans.
        single = {"deepseek-flash": [{"name": "V4.1", "since": t0.isoformat()}]}
        self.assertEqual(
            [name for name, _, _ in noob.deepseek_version_spans(
                single, "deepseek-flash", t0 - dt.timedelta(days=1),
                t0 + dt.timedelta(days=1))],
            ["V4.1"])
        # Repeated display names are not version changes either.
        repeated = {"deepseek-flash": [
            {"name": "V4.1", "since": t0.isoformat()},
            {"name": "V4.1", "since": (t0 + dt.timedelta(days=1)).isoformat()},
            {"name": "V4.5", "since": (t0 + dt.timedelta(days=2)).isoformat()}]}
        self.assertEqual(
            [name for name, _, _ in noob.deepseek_version_spans(
                repeated, "deepseek-flash", t0, t0 + dt.timedelta(days=3))],
            ["V4.1", "V4.5"])

    def test_opencode_usage_splits_rows_by_version(self):
        boundary = dt.datetime(2026, 10, 14, 6, 0, tzinfo=dt.timezone.utc)
        boundary_ms = int(boundary.timestamp() * 1000)

        def panel(miss):
            return {"models": [{"model": {"id": "deepseek-flash", "providerID": "deepseek"},
                                "steps": 1, "tokens": {"input": miss, "output": 0,
                                                       "cache": {"read": 0, "write": 0}}}]}

        def fake_stats(from_ms, to_ms, timeout=20):
            return panel(600_000) if from_ms >= boundary_ms else panel(1_000_000)

        versions = {"deepseek-flash": [
            {"name": "DeepSeek-V4.1-Flash", "since": "2026-10-01T00:00:00+00:00"},
            {"name": "DeepSeek-V4.5-Flash", "since": boundary.isoformat()}]}
        now = dt.datetime(2026, 10, 14, 12, 0, tzinfo=dt.timezone.utc)
        with mock.patch.object(noob, "peak_windows", return_value=[]), \
             mock.patch.object(noob, "opencode_stats", side_effect=fake_stats):
            rows = self.real_opencode_usage(now=now, rates=noob.FALLBACK_DEEPSEEK_RATES,
                                            usd_cny=1.0, versions=versions)
        self.assertEqual(rows["day"]["DeepSeek-V4.1-Flash"]["input"], 400_000)
        self.assertEqual(rows["day"]["DeepSeek-V4.5-Flash"]["input"], 600_000)
        self.assertAlmostEqual(rows["day"]["DeepSeek-V4.1-Flash"]["estimate"], 0.4)
        self.assertAlmostEqual(rows["day"]["DeepSeek-V4.5-Flash"]["estimate"], 0.6)

    def test_opencode_usage_merges_same_name_version_spans(self):
        boundary = dt.datetime(2026, 10, 14, 6, 0, tzinfo=dt.timezone.utc)
        boundary_ms = int(boundary.timestamp() * 1000)

        def panel(miss):
            return {"models": [{"model": {"id": "deepseek-flash", "providerID": "deepseek"},
                                "steps": 1, "tokens": {"input": miss, "output": 0,
                                                       "cache": {"read": 0, "write": 0}}}]}

        def fake_stats(from_ms, to_ms, timeout=20):
            return panel(600_000) if from_ms >= boundary_ms else panel(1_000_000)

        now = dt.datetime(2026, 10, 14, 12, 0, tzinfo=dt.timezone.utc)
        spans = [("DeepSeek-V4.1-Flash", now - dt.timedelta(hours=12), boundary),
                 ("DeepSeek-V4.1-Flash", boundary, now)]
        with mock.patch.object(noob, "peak_windows", return_value=[]), \
             mock.patch.object(noob, "deepseek_version_spans", return_value=spans), \
             mock.patch.object(noob, "opencode_stats", side_effect=fake_stats):
            rows = self.real_opencode_usage(now=now, rates=noob.FALLBACK_DEEPSEEK_RATES,
                                            usd_cny=1.0, versions={})
        row = rows["day"]["DeepSeek-V4.1-Flash"]
        self.assertEqual(row["input"], 1_000_000)
        self.assertAlmostEqual(row["estimate"], 1.0)

    def test_pi_usage_reads_session_logs_and_cost(self):
        # Friday 2026-10-16: 02:30 UTC is inside a peak window, 12:30 UTC is not.
        now = dt.datetime(2026, 10, 16, 20, 0, tzinfo=dt.timezone.utc).astimezone()
        peak_stamp = dt.datetime(2026, 10, 16, 2, 30, tzinfo=dt.timezone.utc)
        off_stamp = dt.datetime(2026, 10, 16, 12, 30, tzinfo=dt.timezone.utc)
        usage = {"input": 100, "output": 50, "cacheRead": 20, "cacheWrite": 5,
                 "reasoning": 10, "totalTokens": 175,
                 "cost": {"total": 999.0}}  # pi's own price is ignored on purpose
        entries = [
            {"type": "session", "id": "s1", "timestamp": off_stamp.isoformat()},
            {"type": "message", "timestamp": peak_stamp.isoformat(), "message": {
                "role": "assistant", "provider": "deepseek", "model": "deepseek-v4-pro",
                "usage": usage}},
            {"type": "message", "timestamp": off_stamp.isoformat(), "message": {
                "role": "assistant", "provider": "deepseek", "model": "deepseek-v4-pro",
                "usage": usage}},
            {"type": "message", "timestamp": off_stamp.isoformat(), "message": {
                "role": "assistant", "provider": "anthropic", "model": "claude-x",
                "usage": {"input": 999, "output": 999, "cacheRead": 0, "cacheWrite": 0,
                          "reasoning": 0, "totalTokens": 1998,
                          "cost": {"total": 5.0}}}},
            {"type": "message", "timestamp": off_stamp.isoformat(), "message": {
                "role": "user", "provider": "deepseek", "model": "deepseek-v4-pro",
                "usage": None}},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            session = Path(tmp) / "proj" / "sess.jsonl"
            session.parent.mkdir(parents=True, exist_ok=True)
            session.write_text("\n".join(json.dumps(e) for e in entries) + "\n",
                               encoding="utf-8")
            with mock.patch.dict(os.environ, {"PI_CODING_AGENT_SESSION_DIR": tmp}, clear=False):
                rows = noob.pi_usage(now=now, usd_cny=7.1)
        row = rows["all"]["deepseek-v4-pro"]
        self.assertEqual(row["input"], 2 * (100 + 20 + 5))
        self.assertEqual(row["cached"], 2 * 20)
        self.assertEqual(row["write"], 2 * 5)
        # `reasoning` is already included in `output`; it must not be added twice.
        self.assertEqual(row["output"], 2 * 50)
        self.assertEqual(row["calls"], 2)
        # deepseek-v4-pro off-peak: 4.5 / 0.15 / 13.5 CNY per 1M tokens, doubled at peak.
        off_peak = (100 * 4.5 + 20 * 0.15 + 50 * 13.5) / 1_000_000
        self.assertAlmostEqual(row["estimate_cny"], off_peak * 3)
        self.assertAlmostEqual(row["estimate"], off_peak * 3 / 7.1)
        # non-deepseek provider and usage-less messages are skipped
        self.assertNotIn("claude-x", rows["all"])
        self.assertEqual(list(rows["all"]), ["deepseek-v4-pro"])
        self.assertEqual(rows["day"], {})  # both stamps are in the previous local day
        self.assertEqual(rows["month"]["deepseek-v4-pro"]["input"], 2 * 125)

    def test_deepseek_peak_classifies_weekday_utc_windows(self):
        utc = dt.timezone.utc
        # Friday inside both windows, and Saturday/Monday-outside at the edges.
        self.assertTrue(noob.deepseek_peak(dt.datetime(2026, 10, 16, 1, 0, tzinfo=utc)))
        self.assertTrue(noob.deepseek_peak(dt.datetime(2026, 10, 16, 9, 59, tzinfo=utc)))
        self.assertFalse(noob.deepseek_peak(dt.datetime(2026, 10, 16, 0, 59, tzinfo=utc)))
        self.assertFalse(noob.deepseek_peak(dt.datetime(2026, 10, 16, 10, 0, tzinfo=utc)))
        self.assertFalse(noob.deepseek_peak(dt.datetime(2026, 10, 16, 4, 0, tzinfo=utc)))
        self.assertFalse(noob.deepseek_peak(dt.datetime(2026, 10, 17, 2, 30, tzinfo=utc)))
        # Local times are converted to UTC before the weekday/hour check.
        local_peak = dt.datetime(2026, 10, 16, 2, 30, tzinfo=utc).astimezone()
        self.assertTrue(noob.deepseek_peak(local_peak))

    def test_merge_deepseek_usage_sums_sources(self):
        pro = {"input": 10, "cached": 2, "write": 1, "output": 5, "calls": 1,
               "estimate": 0.01, "estimate_cny": 0.071}
        more = {"input": 20, "cached": 3, "write": 0, "output": 7, "calls": 2,
                "estimate": 0.02, "estimate_cny": 0.142}
        flash = {"input": 100, "cached": 0, "write": 0, "output": 10, "calls": 1,
                 "estimate": 0.5, "estimate_cny": 3.55}
        merged = noob.merge_deepseek_usage(
            {"day": {"DeepSeek-V4-Pro": pro}},
            {"day": {"DeepSeek-V4-Pro": more, "DeepSeek-V4.1-Flash": flash}})
        self.assertEqual(merged["day"]["DeepSeek-V4-Pro"]["input"], 30)
        self.assertEqual(merged["day"]["DeepSeek-V4-Pro"]["calls"], 3)
        self.assertAlmostEqual(merged["day"]["DeepSeek-V4-Pro"]["estimate"], 0.03)
        self.assertAlmostEqual(merged["day"]["DeepSeek-V4-Pro"]["estimate_cny"], 0.213)
        self.assertEqual(merged["day"]["DeepSeek-V4.1-Flash"]["input"], 100)
        self.assertEqual(merged["week"], {})

    def test_parse_deepseek_prices_reads_both_bands(self):
        html = ("<table><tr><td>MODEL</td><td>deepseek-flash<sup>(1)</sup></td>"
                "<td>deepseek-v4-pro</td></tr>"
                "<tr><td>百万tokens输入<br>（缓存命中）</td><td>空闲时段</td><td>0.02元</td><td>0.15元</td></tr>"
                "<tr><td>高峰时段</td><td>0.04元</td><td>0.30元</td></tr>"
                "<tr><td>百万tokens输入<br>（缓存未命中）</td><td>空闲时段</td><td>1元</td><td>4.5元</td></tr>"
                "<tr><td>高峰时段</td><td>2元</td><td>9.0元</td></tr>"
                "<tr><td>百万tokens输出</td><td>空闲时段</td><td>4元</td><td>13.5元</td></tr>"
                "<tr><td>高峰时段</td><td>8元</td><td>27.0元</td></tr></table>")
        rates = noob.parse_deepseek_prices(html)
        self.assertEqual(rates["deepseek-flash"]["cache_hit"], (0.02, 0.04))
        self.assertEqual(rates["deepseek-flash"]["cache_miss"], (1.0, 2.0))
        self.assertEqual(rates["deepseek-flash"]["output"], (4.0, 8.0))
        self.assertEqual(rates["deepseek-v4-pro"]["output"], (13.5, 27.0))
        for broken in ("<p>no table</p>", "<table><tr><td>x</td></tr></table>"):
            with self.subTest(broken=broken), self.assertRaises(ValueError):
                noob.parse_deepseek_prices(broken)

    def test_deepseek_balance_lines_show_balance_or_hint(self):
        balance = [{"currency": "CNY", "total": 110.0, "granted": 10.0, "topped_up": 100.0}]
        self.assertIn("¥110.00", "".join(noob.deepseek_balance_lines(balance, None, 80, False, 5.0)))
        self.assertIn("set DEEPSEEK_API_KEY",
                      "".join(noob.deepseek_balance_lines(None, "set DEEPSEEK_API_KEY", 80, False)))
        self.assertEqual(noob.deepseek_balance_lines(None, None, 80, False), [])
        for width in (20, 32, 52, 80):
            with self.subTest(width=width):
                for line in noob.deepseek_balance_lines(balance, None, width, False):
                    self.assertLessEqual(len(line), width)
        usage = noob.build_usage([])
        shown = noob.render_home(usage, {}, width=80, deepseek=balance)
        self.assertIn("DEEPSEEK", shown)
        self.assertIn("¥110.00", shown)
        self.assertIn("set DEEPSEEK_API_KEY",
                      noob.render_home(usage, {}, width=80, deepseek_hint="set DEEPSEEK_API_KEY"))
        self.assertNotIn("DEEPSEEK", noob.render_home(usage, {}, width=80))

    def test_deepseek_cost_rows_share_columns_and_wrap_together(self):
        for amounts in ((0.66, 4.29, 22.65, 22.65),
                        (9.99, 10.00, 123.45, 1234.56),
                        (999.99, 1000.00, 9999.99, 10000.00)):
            opencode = dict(zip((p for p, _ in noob.PERIODS), amounts))
            pi = {p: (i + 1) * 0.01 for i, (p, _) in enumerate(noob.PERIODS)}
            source = {p: {"m": {"estimate_cny": opencode[p] + pi[p]}}
                      for p, _ in noob.PERIODS}
            usage = noob.build_usage([("DeepSeek", source)])
            usage["deepseek_split"] = {"opencode": {"cny": opencode}, "pi": {"cny": pi}}
            for color in (False, True):
                for width in (20, 39, 40, *range(75, 101), 120):
                    with self.subTest(amounts=amounts, color=color, width=width):
                        rendered = "\n".join(noob.platform_summary_lines(
                            usage, [], None, None, None, width, color))
                        plain = noob.ANSI_RE.sub("", rendered)
                        lines = plain.splitlines()
                        self.assertLessEqual(max(map(len, lines)), width)
                        rows = [line for line in lines
                                if re.search(r"\b(?:COST|opencode|pi)\b", line)]
                        self.assertEqual(len(rows), 3)
                        if any("·" in row for row in rows):
                            positions = [[i for i, ch in enumerate(row) if ch == "·"]
                                         for row in rows]
                            self.assertEqual(len(positions[0]), 3)
                            self.assertEqual(positions, [positions[0]] * 3)
                            for label in ("today", "week", "month", "all"):
                                self.assertEqual(len({row.index(label) for row in rows}), 1)
                        else:
                            for label in ("today", "week", "month", "all"):
                                period_rows = [line for line in lines if label in line]
                                self.assertEqual(len(period_rows), 3)
                                self.assertEqual(len({line.index(label) for line in period_rows}), 1)
                        if color:
                            self.assertIn("\033[92m", rendered)
                            self.assertIn("\033[2m", rendered)

    def test_deepseek_hint_appears_when_no_key_is_set(self):
        class Terminal(io.StringIO):
            def isatty(self):
                return True

        terminal = Terminal()
        with mock.patch.object(noob.sys, "stdout", terminal), \
             mock.patch.object(noob, "codex_data", return_value={}), \
             mock.patch.object(noob, "scan_local_sessions", return_value={}), \
             mock.patch.object(noob, "wait_for_refresh", side_effect=KeyboardInterrupt):
            self.assertEqual(noob.main([]), 0)
        self.assertIn("set DEEPSEEK_API_KEY", terminal.getvalue())

    def test_deepseek_balance_is_queried_and_passed_to_the_card(self):
        class Terminal(io.StringIO):
            def isatty(self):
                return True

        terminal = Terminal()
        balance = [{"currency": "CNY", "total": 110.0, "granted": 10.0, "topped_up": 100.0}]
        saved = Path("/Users/eke/Downloads/noobsolo-usage-deepseek-test.png")

        def save_once(*args, **kwargs):
            kwargs["on_key"]("s")
            raise KeyboardInterrupt

        with mock.patch.dict(os.environ, {"DEEPSEEK_API_KEY": "sk-test"}), \
             mock.patch.object(noob.sys, "stdout", terminal), \
             mock.patch.object(noob, "codex_data", return_value={}), \
             mock.patch.object(noob, "scan_local_sessions", return_value={}), \
             mock.patch.object(noob, "fetch_usd_cny", return_value=5.0), \
             mock.patch.object(noob, "deepseek_balance", return_value=balance) as query, \
             mock.patch.object(noob, "save_card", return_value=saved) as card, \
             mock.patch.object(noob, "wait_for_refresh", side_effect=save_once):
            self.assertEqual(noob.main([]), 0)
        query.assert_called_once_with("sk-test")
        self.assertIn("DEEPSEEK", terminal.getvalue())
        self.assertIn("¥110.00", terminal.getvalue())
        self.assertEqual(card.call_args.kwargs["deepseek"], "¥110.00")
        self.assertIn("totals", card.call_args.args[0])

    def test_deepseek_failure_is_a_warning_and_marks_balance_unavailable(self):
        class Terminal(io.StringIO):
            def isatty(self):
                return True

        terminal = Terminal()
        with mock.patch.dict(os.environ, {"DEEPSEEK_API_KEY": "sk-test"}), \
             mock.patch.object(noob.sys, "stdout", terminal), \
             mock.patch.object(noob, "codex_data", return_value={}), \
             mock.patch.object(noob, "scan_local_sessions", return_value={}), \
             mock.patch.object(noob, "fetch_usd_cny", return_value=7.1), \
             mock.patch.object(noob, "deepseek_balance",
                               side_effect=RuntimeError("boom")):
            self.assertEqual(noob.main(["--once"]), 1)
        self.assertIn("unavailable", terminal.getvalue())
        self.assertIn("DeepSeek balance query failed: boom", terminal.getvalue())

    def test_keychain_secret_reads_password_and_handles_failures(self):
        completed = lambda code, out="": mock.Mock(returncode=code, stdout=out, stderr="")
        with mock.patch.object(noob.sys, "platform", "darwin"), \
             mock.patch.object(noob.subprocess, "run",
                               return_value=completed(0, "sk-key\n")) as run:
            self.assertEqual(self.real_keychain_secret("noob-deepseek"), "sk-key")
        self.assertEqual(run.call_args.args[0],
                         ["security", "find-generic-password", "-s", "noob-deepseek", "-w"])
        with mock.patch.object(noob.sys, "platform", "darwin"), \
             mock.patch.object(noob.subprocess, "run", return_value=completed(44)):
            self.assertIsNone(self.real_keychain_secret("noob-deepseek"))
        with mock.patch.object(noob.sys, "platform", "darwin"), \
             mock.patch.object(noob.subprocess, "run", side_effect=FileNotFoundError):
            self.assertIsNone(self.real_keychain_secret("noob-deepseek"))
        with mock.patch.object(noob.sys, "platform", "linux"), \
             mock.patch.object(noob.subprocess, "run") as run:
            self.assertIsNone(self.real_keychain_secret("noob-deepseek"))
            run.assert_not_called()

    def test_deepseek_api_key_prefers_environment_over_keychain(self):
        with mock.patch.dict(os.environ, {"DEEPSEEK_API_KEY": "sk-env"}), \
             mock.patch.object(noob, "keychain_secret", return_value="sk-chain") as chain:
            self.assertEqual(noob.deepseek_api_key(), "sk-env")
            chain.assert_not_called()
        self.assertIsNone(os.environ.get("DEEPSEEK_API_KEY"))
        with mock.patch.object(noob, "keychain_secret", return_value="sk-chain") as chain:
            self.assertEqual(noob.deepseek_api_key(), "sk-chain")
            chain.assert_called_once_with(noob.KEYCHAIN_SERVICE)

    def test_deepseek_balance_sits_under_the_codex_bars(self):
        limits = {"rateLimits": {"limitId": "codex", "primary": {
            "usedPercent": 40, "windowDurationMins": 300, "resetsAt": 1700000000}}}
        balance = [{"currency": "CNY", "total": 110.0, "granted": 10.0, "topped_up": 100.0}]
        usage = noob.build_usage([])
        lines = noob.render_home(usage, limits, width=80, deepseek=balance).splitlines()
        bar_index = next(i for i, line in enumerate(lines) if "5H" in line and "▰" in line)
        deepseek_index = next(i for i, line in enumerate(lines) if "DEEPSEEK" in line)
        total_index = next(i for i, line in enumerate(lines) if line.strip() == "TOTAL")
        self.assertGreater(deepseek_index, bar_index)
        self.assertLess(deepseek_index, total_index)
        fallback = noob.render_home(usage, {}, width=80, deepseek=balance).splitlines()
        platform_index = next(i for i, line in enumerate(fallback) if "PLATFORMS" in line)
        fallback_index = next(i for i, line in enumerate(fallback) if "DEEPSEEK" in line)
        self.assertGreater(fallback_index, platform_index)

    def test_total_cost_and_token_separators_share_columns(self):
        values = {
            "day": (10.89, 57_030_000),
            "week": (158.51, 672_560_000),
            "month": (423.71, 1_973_480_000),
            "all": (468.86, 2_080_670_000),
        }
        source = {}
        for period, (cost, tokens) in values.items():
            row = noob.empty_local_row()
            row.update({"input": tokens - 1000, "output": 1000, "estimate": cost})
            source[period] = {"gpt-6-sol": row}
        usage = noob.build_usage([("Codex", source)])
        lines = noob.render_home(usage, {}, width=96).splitlines()
        cost_line = next(line for line in lines if line.strip().startswith("COST"))
        token_line = next(line for line in lines if line.strip().startswith("tokens"))
        cost_separators = [index for index, char in enumerate(cost_line) if char == "·"]
        token_separators = [index for index, char in enumerate(token_line) if char == "·"]
        self.assertEqual(len(cost_separators), 3)
        self.assertEqual(cost_separators, token_separators)

    def test_opencode_usage_filters_deepseek_and_merges(self):
        payload = {"data": {"models": [
            {"model": {"id": "deepseek-flash", "providerID": "deepseek"}, "steps": 3,
             "tokens": {"input": 1000, "output": 200, "reasoning": 50,
                        "cache": {"read": 400, "write": 10}}, "cost": 0.00123},
            {"model": {"id": "gpt-6-sol", "providerID": "openai"}, "steps": 9,
             "tokens": {"input": 5, "output": 5, "reasoning": 0,
                        "cache": {"read": 0, "write": 0}}, "cost": 99.0}]}}

        def fake_run(command, **kwargs):
            return mock.Mock(returncode=0, stdout=json.dumps(payload), stderr="")

        with mock.patch.object(noob, "peak_windows", return_value=[]), \
             mock.patch.object(noob.subprocess, "run", side_effect=fake_run) as run:
            rows = self.real_opencode_usage(now=dt.datetime(2026, 10, 14, 12, 0,
                                                             tzinfo=dt.timezone.utc))
        self.assertEqual(run.call_count, 4)
        self.assertTrue(run.call_args.args[0][3].startswith(noob.OPENCODE_STATS_PATH + "?from="))
        row = rows["day"]["deepseek-flash"]
        self.assertEqual((row["input"], row["cached"], row["write"], row["output"]),
                         (1410, 400, 10, 250))
        self.assertEqual(row["calls"], 3)
        expected = noob.deepseek_token_cost(
            noob.FALLBACK_DEEPSEEK_RATES["deepseek-flash"], 1000, 400, 250, False) / noob.FALLBACK_USD_CNY
        self.assertAlmostEqual(row["estimate"], expected)
        self.assertNotIn("gpt-6-sol", rows["day"])

        usage = noob.build_usage([("DeepSeek", rows)])
        self.assertEqual(usage["totals"]["day"]["tokens"], 1660)
        detail = noob.render_models(usage, width=80)
        self.assertIn("deepseek-flash", detail)
        self.assertNotIn("gpt-6-sol", detail)

    def test_peak_windows_are_utc_weekday_hours(self):
        wed = dt.datetime(2026, 10, 14, 0, 0, tzinfo=dt.timezone.utc)
        self.assertEqual(len(noob.peak_windows(wed, wed + dt.timedelta(hours=12))), 2)
        sat = dt.datetime(2026, 10, 17, 0, 0, tzinfo=dt.timezone.utc)
        self.assertEqual(noob.peak_windows(sat, sat + dt.timedelta(hours=12)), [])
        self.assertEqual(noob.peak_windows(None, wed), [])

    def test_opencode_usage_prices_peak_tokens_at_double(self):
        def panel(miss):
            return {"models": [{"model": {"id": "deepseek-flash", "providerID": "deepseek"},
                                "steps": 1, "tokens": {"input": miss, "output": 0,
                                                       "cache": {"read": 0, "write": 0}}}]}

        window = (1_700_000_000_000, 1_700_010_000_000)

        def fake_stats(from_ms, to_ms, timeout=20):
            return panel(400_000) if (from_ms, to_ms) == window else panel(1_000_000)

        now = dt.datetime(2026, 10, 14, 12, 0, tzinfo=dt.timezone.utc)
        with mock.patch.object(noob, "peak_windows", return_value=[window]), \
             mock.patch.object(noob, "opencode_stats", side_effect=fake_stats):
            rows = self.real_opencode_usage(now=now, rates=noob.FALLBACK_DEEPSEEK_RATES,
                                            usd_cny=1.0)
        # 600k off-peak cache-miss at ¥1/M + 400k peak cache-miss at ¥2/M = ¥1.40.
        self.assertAlmostEqual(rows["day"]["deepseek-flash"]["estimate"], 1.4)

    def test_opencode_usage_leaves_unknown_models_unpriced(self):
        payload = {"models": [{"model": {"id": "deepseek-v9-flash", "providerID": "deepseek"},
                               "steps": 1, "tokens": {"input": 1_000_000, "output": 0,
                                                      "cache": {"read": 0, "write": 0}}}]}

        def fake_stats(from_ms, to_ms, timeout=20):
            return payload

        now = dt.datetime(2026, 10, 14, 12, 0, tzinfo=dt.timezone.utc)
        with mock.patch.object(noob, "peak_windows", return_value=[]), \
             mock.patch.object(noob, "opencode_stats", side_effect=fake_stats):
            rows = self.real_opencode_usage(now=now, rates=noob.FALLBACK_DEEPSEEK_RATES,
                                            usd_cny=1.0)
        row = rows["day"]["deepseek-v9-flash"]
        self.assertEqual(row["input"], 1_000_000)
        self.assertEqual(row["unpriced"], 1)
        self.assertEqual(row["estimate"], 0.0)

    def test_opencode_usage_maps_retired_flash_alias_to_flash_price(self):
        payload = {"models": [{"model": {"id": "deepseek-v4-flash", "providerID": "deepseek"},
                               "steps": 1, "tokens": {"input": 1_000_000, "output": 0,
                                                      "cache": {"read": 0, "write": 0}}}]}

        def fake_stats(from_ms, to_ms, timeout=20):
            return payload

        now = dt.datetime(2026, 10, 14, 12, 0, tzinfo=dt.timezone.utc)
        with mock.patch.object(noob, "peak_windows", return_value=[]), \
             mock.patch.object(noob, "opencode_stats", side_effect=fake_stats):
            rows = self.real_opencode_usage(now=now, rates=noob.FALLBACK_DEEPSEEK_RATES,
                                            usd_cny=1.0)
        self.assertAlmostEqual(rows["day"]["deepseek-v4-flash"]["estimate"], 1.0)

    def test_opencode_stats_rejects_bad_payloads(self):
        with mock.patch.object(noob.subprocess, "run",
                               return_value=mock.Mock(returncode=1, stdout="", stderr="boom")):
            with self.assertRaises(RuntimeError):
                noob.opencode_stats(0, 1)
        with mock.patch.object(noob.subprocess, "run",
                               return_value=mock.Mock(returncode=0, stdout="{}", stderr="")):
            with self.assertRaises(RuntimeError):
                noob.opencode_stats(0, 1)

    def test_opencode_usage_skips_empty_day_range_at_midnight(self):
        payload = {"data": {"models": []}}

        def fake_run(command, **kwargs):
            return mock.Mock(returncode=0, stdout=json.dumps(payload), stderr="")

        now = dt.datetime(2026, 10, 14, 0, 0, 0, tzinfo=dt.timezone.utc)
        with mock.patch.object(noob.subprocess, "run", side_effect=fake_run) as run:
            rows = self.real_opencode_usage(now=now)
        self.assertEqual(run.call_count, 3)
        self.assertEqual(rows["day"], {})

    def test_opencode_failure_is_reported_in_status(self):
        class Terminal(io.StringIO):
            def isatty(self):
                return True

        terminal = Terminal()
        with mock.patch.object(noob, "opencode_usage", side_effect=RuntimeError("no service")), \
             mock.patch.object(noob.shutil, "which", return_value="/usr/bin/opencode"), \
             mock.patch.object(noob.sys, "stdout", terminal), \
             mock.patch.object(noob, "codex_data", return_value={}), \
             mock.patch.object(noob, "scan_local_sessions", return_value={}):
            self.assertEqual(noob.main(["--once"]), 1)
        self.assertIn("OpenCode usage query failed: no service", terminal.getvalue())

    def test_opencode_is_skipped_when_not_installed(self):
        class Terminal(io.StringIO):
            def isatty(self):
                return True

        with mock.patch.object(noob, "opencode_usage") as usage, \
             mock.patch.object(noob.shutil, "which", return_value=None), \
             mock.patch.object(noob.sys, "stdout", Terminal()), \
             mock.patch.object(noob, "codex_data", return_value={}), \
             mock.patch.object(noob, "scan_local_sessions", return_value={}):
            noob.main(["--once"])
        usage.assert_not_called()

    def test_usage_card_includes_deepseek_balance(self):
        usage = noob.build_usage([])
        image = noob_card.card_svg(usage, deepseek="$16.43")
        root = ET.fromstring(image)
        node = root.find(".//*[@id='deepseek-balance']")
        self.assertEqual(node.text, "$16.43")
        # The balance sits below the 5H/WEEK quota bars, at the card's bottom edge.
        week_bar = float(root.find(".//*[@id='quota-week-fill']").get("y"))
        self.assertGreater(float(node.get("y")), week_bar)
        self.assertEqual(node.get("text-anchor"), "end")
        # The footer is a balance section with one CODEX row and one DEEPSEEK row.
        self.assertIn(">BALANCE<", image)
        self.assertIn(">CODEX<", image)
        self.assertNotIn("USAGE · API-EQUIVALENT ESTIMATE", image)
        self.assertNotIn("Not a subscription charge", image)
        self.assertNotIn("DEEPSEEK BALANCE", image)
        blank = ET.fromstring(noob_card.card_svg(usage))
        self.assertEqual(blank.find(".//*[@id='deepseek-balance']").text, "—")


    def test_k_key_opens_models_view_and_saves_models_card(self):
        class Terminal(io.StringIO):
            def isatty(self):
                return True

        terminal = Terminal()
        source = {"day": {"gpt-6-sol": {"input": 1000, "cached": 0, "write": 0,
                                        "output": 100, "estimate": 1.0, "unpriced": 0,
                                        "calls": 1}}}
        saved = Path("/Users/eke/Downloads/noobsolo-models-test.png")

        def act(*args, **kwargs):
            kwargs["on_key"]("k")
            kwargs["on_key"]("s")
            raise KeyboardInterrupt

        with mock.patch.object(noob.sys, "stdout", terminal), \
             mock.patch.object(noob, "codex_data", return_value={}), \
             mock.patch.object(noob, "scan_local_sessions", return_value=source), \
             mock.patch.object(noob, "save_models_card", return_value=saved) as card, \
             mock.patch.object(noob, "wait_for_refresh", side_effect=act):
            self.assertEqual(noob.main([]), 0)
        card.assert_called_once()
        self.assertEqual(card.call_args.kwargs, {"sort_period": "day", "sort_desc": True})
        self.assertIn("MODEL", terminal.getvalue())
        self.assertIn("Saved models card", terminal.getvalue())

    def test_sort_keys_order_the_models_view(self):
        class Terminal(io.StringIO):
            def isatty(self):
                return True

        terminal = Terminal()

        def row(cost):
            return {"input": 1000, "cached": 0, "write": 0, "output": 100,
                    "estimate": cost, "unpriced": 0, "calls": 1}

        source = {"day": {"gpt-6-sol": row(9.0), "gpt-5.5": row(1.0)},
                  "week": {"gpt-6-sol": row(1.0), "gpt-5.5": row(9.0)},
                  "month": {"gpt-6-sol": row(5.0), "gpt-5.5": row(4.0)},
                  "all": {"gpt-6-sol": row(2.0), "gpt-5.5": row(8.0)}}

        def act(*args, **kwargs):
            kwargs["on_key"]("k")   # open the models view
            kwargs["on_key"]("w")   # week, descending
            kwargs["on_key"]("W")   # same key again flips to ascending
            kwargs["on_key"]("m")   # month: sorts here instead of saving a card
            raise KeyboardInterrupt

        with mock.patch.object(noob.sys, "stdout", terminal), \
             mock.patch.object(noob, "codex_data", return_value={}), \
             mock.patch.object(noob, "scan_local_sessions", return_value=source), \
             mock.patch.object(noob, "save_reset_card") as reset_card, \
             mock.patch.object(noob, "wait_for_refresh", side_effect=act):
            self.assertEqual(noob.main([]), 0)
        output = terminal.getvalue()
        self.assertIn("WEEK ↓", output)
        self.assertIn("WEEK ↑", output)
        self.assertIn("MONTH ↓", output)
        self.assertNotIn("TODAY ↓", output[output.index("WEEK ↓"):])
        reset_card.assert_not_called()

    def test_models_card_sorts_by_viewed_period(self):
        def row(cost):
            return {"input": 1000, "cached": 0, "write": 0, "output": 100,
                    "estimate": cost, "unpriced": 0, "calls": 1}

        source = {"day": {"a-sol": row(1.0), "b-sol": row(5.0)},
                  "week": {"a-sol": row(9.0), "b-sol": row(2.0)}}
        usage = noob.build_usage([("Codex", source)])

        def names(root):
            return [element.text for element in root.find(".//*[@id='model-rows']")
                    if element.tag.endswith("text") and element.get("x") == "88"]

        descending = ET.fromstring(noob_card.models_card_svg(
            usage, sort_period="week", sort_desc=True))
        self.assertEqual(names(descending), ["A-SOL", "B-SOL"])
        self.assertEqual(descending.find(".//*[@id='head-week']").text, "WEEK ↓")

        ascending = ET.fromstring(noob_card.models_card_svg(
            usage, sort_period="week", sort_desc=False))
        self.assertEqual(names(ascending), ["B-SOL", "A-SOL"])
        self.assertEqual(ascending.find(".//*[@id='head-week']").text, "WEEK ↑")
        self.assertEqual(ascending.find(".//*[@id='head-month']").text, "MONTH")

    def test_models_card_lists_models_and_costs(self):
        def row(cost):
            return {"input": 1000, "cached": 0, "write": 0, "output": 100,
                    "estimate": cost, "unpriced": 0, "calls": 1}

        source = {"day": {"gpt-6-sol": row(3.0), "deepseek-flash": row(0.5)},
                  "all": {"gpt-6-sol": row(3.0), "deepseek-flash": row(0.5)}}
        usage = noob.build_usage([("Codex", source)])
        root = ET.fromstring(noob_card.models_card_svg(usage))
        rows = root.find(".//*[@id='model-rows']")
        texts = [element.text for element in rows if element.tag.endswith("text")]
        self.assertIn("GPT-6-SOL", texts)
        self.assertIn("DEEPSEEK-FLASH", texts)
        self.assertEqual(root.find(".//*[@id='card-status']").text, "2 MODELS")

    def test_daily_cards_saves_usage_and_models_cards_for_the_day(self):
        source = {"day": {"gpt-6-sol": {"input": 1000, "cached": 0, "write": 0,
                                        "output": 100, "estimate": 1.0, "unpriced": 0,
                                        "calls": 1}}}
        limits = {"rateLimits": {"limitId": "codex", "primary": {
            "usedPercent": 20, "windowDurationMins": 300}, "secondary": {
            "usedPercent": 61, "windowDurationMins": 10080}}}
        usage_path = Path("/tmp/archive/noobsolo-usage-20261021.png")
        models_path = Path("/tmp/archive/noobsolo-models-20261021.png")
        moment = dt.datetime(2026, 10, 21, 0, 0, 5,
                             tzinfo=dt.timezone(dt.timedelta(hours=8)))
        with mock.patch.object(noob, "scan_local_sessions", return_value=source), \
             mock.patch.object(noob, "codex_data", return_value=limits), \
             mock.patch.object(noob, "save_card", return_value=usage_path) as card, \
             mock.patch.object(noob, "save_models_card", return_value=models_path) as models:
            paths, warnings = noob.daily_cards(directory="/tmp/archive", now=moment)
        self.assertEqual(paths, [usage_path, models_path])
        self.assertEqual(warnings, [])
        self.assertEqual(card.call_args.kwargs["directory"], Path("/tmp/archive"))
        self.assertEqual(card.call_args.kwargs["now"], moment)
        self.assertEqual(card.call_args.kwargs["name"], "noobsolo-usage-20261021.png")
        self.assertEqual(card.call_args.kwargs["quotas"], {300: (80, "32"), 10080: (39, "33")})
        self.assertEqual(models.call_args.kwargs["name"], "noobsolo-models-20261021.png")
        self.assertEqual(models.call_args.kwargs["sort_period"], "month")
        self.assertTrue(models.call_args.kwargs["sort_desc"])

    def test_daily_cards_cli_prints_both_paths(self):
        first = Path("/tmp/archive/noobsolo-usage-20261021.png")
        second = Path("/tmp/archive/noobsolo-models-20261021.png")
        output = io.StringIO()
        with mock.patch.object(noob, "daily_cards", return_value=([first, second], [])) as capture, \
             mock.patch.object(noob.sys, "stdout", output):
            self.assertEqual(noob.main(["--daily-cards", "--daily-dir", "/tmp/archive"]), 0)
        capture.assert_called_once_with("/tmp/archive")
        self.assertIn("Saved: %s" % first, output.getvalue())
        self.assertIn("Saved: %s" % second, output.getvalue())

    def test_install_daily_schedule_writes_launch_agent(self):
        with tempfile.TemporaryDirectory() as folder:
            agent = Path(folder) / "LaunchAgents" / "com.noob.dailycards.plist"
            with mock.patch.object(noob, "launchctl",
                                   return_value=subprocess.CompletedProcess([], 0, "", "")) as control:
                path = noob.install_daily_schedule("23:30", agent_path=agent,
                                                   python="/usr/bin/python3",
                                                   script="/tmp/noob.py")
            contents = plistlib.loads(path.read_bytes())
            self.assertTrue(agent.is_file())
        self.assertEqual(contents["Label"], "com.noob.dailycards")
        self.assertEqual(contents["StartCalendarInterval"], {"Hour": 23, "Minute": 30})
        self.assertEqual(contents["ProgramArguments"],
                         ["/usr/bin/python3", str(Path("/tmp/noob.py").resolve()),
                          "--daily-cards"])
        self.assertIn("/usr/bin", contents["EnvironmentVariables"]["PATH"].split(os.pathsep))
        self.assertFalse(contents["RunAtLoad"])
        self.assertEqual(control.call_count, 2)
        self.assertEqual(noob.launch_plist(directory="/tmp/archive")["ProgramArguments"][-2:],
                         ["--daily-dir", "/tmp/archive"])

    def test_launch_agent_reuses_the_running_python(self):
        with mock.patch.object(noob.sys, "executable", "/opt/conda/bin/python3.14"):
            arguments = noob.launch_plist()["ProgramArguments"]
        self.assertEqual(arguments[0], "/opt/conda/bin/python3.14")

    def test_install_daily_schedule_falls_back_to_legacy_launchctl(self):
        failures = [subprocess.CompletedProcess([], 1, "", "bootstrap failed"),
                    subprocess.CompletedProcess([], 1, "", "bootstrap failed"),
                    subprocess.CompletedProcess([], 1, "", "load failed")]
        with tempfile.TemporaryDirectory() as folder:
            with mock.patch.object(noob, "launchctl", side_effect=failures):
                with self.assertRaises(RuntimeError):
                    noob.install_daily_schedule(
                        "00:00", agent_path=Path(folder) / "agent.plist")

    def test_uninstall_daily_schedule_removes_the_agent(self):
        with tempfile.TemporaryDirectory() as folder:
            agent = Path(folder) / "agent.plist"
            agent.write_text("stale")
            with mock.patch.object(noob, "launchctl") as control:
                removed = noob.uninstall_daily_schedule(agent_path=agent)
        self.assertEqual(removed, agent)
        self.assertFalse(agent.exists())
        self.assertEqual(control.call_args.args[0], "bootout")

    def test_dashboard_captures_cards_when_the_day_rolls_over(self):
        class Terminal(io.StringIO):
            def isatty(self):
                return True

        real_date = dt.date

        class Day(real_date):
            calls = 0

            @classmethod
            def today(cls):
                Day.calls += 1
                if Day.calls == 1:
                    return real_date(2026, 10, 20)
                return real_date(2026, 10, 21)

        terminal = Terminal()
        saved = [Path("/tmp/archive/noobsolo-usage-20261021.png"),
                 Path("/tmp/archive/noobsolo-models-20261021.png")]
        with mock.patch.object(noob.sys, "stdout", terminal), \
             mock.patch.object(noob, "codex_data", return_value={}), \
             mock.patch.object(noob, "scan_local_sessions", return_value={}), \
             mock.patch.object(noob.dt, "date", Day), \
             mock.patch.object(noob, "daily_cards", return_value=(saved, [])) as capture, \
             mock.patch.object(noob, "wait_for_refresh", side_effect=KeyboardInterrupt):
            self.assertEqual(noob.main([]), 0)
        capture.assert_called_once_with()
        self.assertIn("Saved nightly cards", terminal.getvalue())

    def test_parse_clock_rejects_bad_times(self):
        self.assertEqual(noob.parse_clock("00:00"), (0, 0))
        self.assertEqual(noob.parse_clock(" 7:05 "), (7, 5))
        for value in ("24:00", "12:60", "midnight", "12", ""):
            with self.assertRaises(ValueError):
                noob.parse_clock(value)


@unittest.skipUnless(shutil.which("git"), "git is required for update tests")
class AutoUpdateTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root)
        self.seed = self.root / "seed"
        self.remote = self.root / "remote.git"
        self.work = self.root / "work"
        self.seed.mkdir()
        self.git("init", "--quiet", "-b", "main", cwd=self.seed)
        (self.seed / "noob.py").write_text("version = 1\n")
        self.git("add", "noob.py", cwd=self.seed)
        self.git("commit", "--quiet", "-m", "first", cwd=self.seed)
        self.git("clone", "--quiet", "--bare", str(self.seed), str(self.remote),
                 cwd=self.root)
        self.git("remote", "add", "origin", str(self.remote), cwd=self.seed)
        self.git("push", "--quiet", "-u", "origin", "main", cwd=self.seed)
        self.git("clone", "--quiet", str(self.remote), str(self.work), cwd=self.root)

    def git(self, *args, cwd, check=True):
        env = dict(os.environ)
        env.update({
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_SYSTEM": os.devnull,
            "GIT_AUTHOR_NAME": "Noob",
            "GIT_AUTHOR_EMAIL": "noob@example.com",
            "GIT_COMMITTER_NAME": "Noob",
            "GIT_COMMITTER_EMAIL": "noob@example.com",
        })
        return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True,
                              check=check, env=env)

    def publish(self, text):
        (self.seed / "noob.py").write_text(text)
        self.git("commit", "--quiet", "-am", "next", cwd=self.seed)
        self.git("push", "--quiet", "origin", "main", cwd=self.seed)

    def test_fast_forwards_a_clean_checkout_to_the_upstream(self):
        old = (self.work / "noob.py").read_text()
        self.publish("version = 2\n")
        result = noob.check_for_update(self.work)
        self.assertEqual(result["status"], "updated")
        self.assertEqual(result["commits"], 1)
        self.assertEqual((self.work / "noob.py").read_text(), "version = 2\n")
        self.assertNotEqual(result["previous"], result["current"])
        self.assertNotEqual(old, (self.work / "noob.py").read_text())

    def test_matching_upstream_reports_current(self):
        result = noob.check_for_update(self.work)
        self.assertEqual(result["status"], "current")
        self.assertEqual(result["commits"], 0)
        self.assertEqual(result["previous"], result["current"])

    def test_local_changes_are_left_alone(self):
        self.publish("version = 2\n")
        (self.work / "noob.py").write_text("version = 99\n")
        result = noob.check_for_update(self.work)
        self.assertEqual(result["status"], "skipped")
        self.assertIn("local changes", result["reason"])
        self.assertEqual((self.work / "noob.py").read_text(), "version = 99\n")

    def test_local_commits_are_not_rewritten(self):
        (self.work / "noob.py").write_text("version = 3\n")
        self.git("commit", "--quiet", "-am", "local", cwd=self.work)
        head = self.git("rev-parse", "HEAD", cwd=self.work).stdout
        self.publish("version = 2\n")
        result = noob.check_for_update(self.work)
        self.assertEqual(result["status"], "skipped")
        self.assertIn("diverge", result["reason"])
        self.assertEqual(self.git("rev-parse", "HEAD", cwd=self.work).stdout, head)

    def test_detached_head_is_skipped(self):
        self.git("checkout", "--quiet", "--detach", "HEAD", cwd=self.work)
        result = noob.check_for_update(self.work)
        self.assertEqual(result["status"], "skipped")
        self.assertIn("detached", result["reason"])

    def test_branch_without_upstream_is_skipped(self):
        self.git("switch", "--quiet", "-c", "topic", cwd=self.work)
        result = noob.check_for_update(self.work)
        self.assertEqual(result["status"], "skipped")
        self.assertIn("no upstream", result["reason"])

    def test_plain_folder_is_not_a_checkout(self):
        plain = self.root / "plain"
        plain.mkdir()
        result = noob.check_for_update(plain)
        self.assertEqual(result["status"], "skipped")
        self.assertIn("not a git checkout", result["reason"])

    def test_update_flag_reports_and_exits(self):
        result = {"status": "current", "current": "abcdef1234567", "commits": 0}
        with mock.patch.object(noob, "check_for_update", return_value=result), \
             mock.patch.object(noob.sys, "stdout", io.StringIO()) as out:
            self.assertEqual(noob.main(["--update"]), 0)
        self.assertIn("up to date", out.getvalue())

    def test_successful_update_restarts_into_the_new_code(self):
        result = {"status": "updated", "previous": "1111111aaaa", "current": "2222222bbbb",
                  "commits": 2}
        with mock.patch.dict(os.environ, {}, clear=False), \
             mock.patch.object(noob, "check_for_update", return_value=result), \
             mock.patch.object(noob.os, "execve") as execve, \
             mock.patch.object(noob.sys, "stderr", io.StringIO()):
            os.environ.pop("NOOB_NO_UPDATE", None)
            os.environ.pop(noob.UPDATE_RESTARTED_ENV, None)
            noob.auto_update_on_start(["--once"])
        path, arguments, env = execve.call_args.args
        self.assertEqual(path, sys.executable)
        self.assertEqual(arguments[1], str(Path(noob.__file__).resolve()))
        self.assertEqual(arguments[2:], ["--once"])
        self.assertEqual(env[noob.UPDATE_RESTARTED_ENV], "1")
        self.assertIn("2 new commits", env[noob.UPDATE_NOTICE_ENV])

    def test_no_update_environment_disables_the_check(self):
        with mock.patch.dict(os.environ, {"NOOB_NO_UPDATE": "1"}), \
             mock.patch.object(noob, "check_for_update") as check:
            self.assertIsNone(noob.auto_update_on_start([]))
        check.assert_not_called()


if __name__ == "__main__":
    unittest.main()
