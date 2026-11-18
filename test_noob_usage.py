import datetime as dt
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import noob
import noob_usage
from noob_usage import UsageLedger, empty_state


NOW = dt.datetime(2026, 10, 29, 12, 0, tzinfo=dt.timezone.utc)


def context():
    return {
        "price_source": "live",
        "pricing_note": None,
        "usd_cny": 7.1,
        "usd_cny_source": "live",
        "deepseek_rates": dict(noob.FALLBACK_DEEPSEEK_RATES),
        "deepseek_source": "fallback",
        "versions": {},
        "deepseek_key": None,
    }


def codex_event(day, model="gpt-6.1-sol", input_tokens=1000, cached=0, output=100,
                cost=0.01, backfill=False, source="codex"):
    return {
        "at": NOW.isoformat(), "day": day, "platform": "Codex", "source": source,
        "model": model, "display": model, "kind": "short",
        "tokens": {"input": input_tokens, "cached": cached, "write": 0, "output": output},
        "cost": cost, "currency": "USD", "unpriced": 0, "calls": 1,
        "rates": {"input": 2.0, "cached": 0.1, "write": 2.5, "output": 10.0,
                  "currency": "USD", "source": "live", "captured_at": NOW.isoformat()},
        "backfill": backfill,
    }


def deepseek_event(day, display="DeepSeek V4.1 Flash", source="opencode",
                   input_tokens=1000, cached=0, output=100, cost_cny=0.5):
    return {
        "at": NOW.isoformat(), "day": day, "platform": "DeepSeek", "source": source,
        "model": "deepseek-flash", "display": display, "kind": "offpeak",
        "tokens": {"input": input_tokens, "cached": cached, "write": 0, "output": output},
        "cost_cny": cost_cny, "cost_usd": cost_cny / 7.1, "usd_cny": 7.1,
        "currency": "CNY", "unpriced": 0, "calls": 1,
        "rates": {"kind": "offpeak", "currency": "CNY", "input": 1.0,
                  "cached": 0.02, "output": 4.0,
                  "source": "live", "captured_at": NOW.isoformat()},
        "backfill": False,
    }


class LedgerTest(unittest.TestCase):
    def test_aggregates_periods_and_sources(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = UsageLedger(tmp)
            with ledger.transaction() as (state, events):
                events.extend([
                    codex_event("2026-10-29", input_tokens=1000, cached=500, output=100,
                                cost=1.5),
                    codex_event("2026-10-28", input_tokens=2000, cached=0, output=200,
                                cost=2.5),
                    deepseek_event("2026-10-29", source="opencode"),
                    deepseek_event("2026-10-29", source="pi", display="DeepSeek Flash",
                                   cost_cny=0.2),
                    deepseek_event("2026-09-30", source="opencode", cost_cny=4.0),
                ])
            rows = ledger.aggregate(NOW)
            self.assertEqual(rows["Codex"]["day"]["gpt-6.1-sol"]["input"], 1000)
            self.assertEqual(rows["Codex"]["week"]["gpt-6.1-sol"]["input"], 3000)
            self.assertEqual(rows["Codex"]["month"]["gpt-6.1-sol"]["estimate"], 4.0)
            self.assertEqual(rows["Codex"]["all"]["gpt-6.1-sol"]["estimate"], 4.0)
            self.assertEqual(rows["DeepSeek"]["month"]["DeepSeek V4.1 Flash"]["input"], 1000)
            self.assertEqual(rows["DeepSeek"]["month"]["DeepSeek V4.1 Flash"]["estimate_cny"], 0.5)
            self.assertIn("DeepSeek Flash", rows["DeepSeek"]["day"])
            self.assertEqual(rows["DeepSeek"]["all"]["DeepSeek V4.1 Flash"]["estimate_cny"], 4.5)
            self.assertEqual(ledger.period_rows("DeepSeek", NOW, source="pi")["day"]
                             ["DeepSeek Flash"]["estimate_cny"], 0.2)

    def test_periods_follow_local_calendar_boundaries(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = UsageLedger(tmp)
            with ledger.transaction() as (state, events):
                events.extend([codex_event("2026-09-30", cost=1.0),
                               codex_event("2026-10-01", cost=2.0),
                               codex_event("2026-10-26", cost=4.0),
                               codex_event("2026-10-29", cost=8.0)])
            rows = ledger.aggregate(NOW)["Codex"]
            self.assertEqual(rows["day"]["gpt-6.1-sol"]["estimate"], 8.0)
            self.assertEqual(rows["week"]["gpt-6.1-sol"]["estimate"], 12.0)
            self.assertEqual(rows["month"]["gpt-6.1-sol"]["estimate"], 14.0)
            self.assertEqual(rows["all"]["gpt-6.1-sol"]["estimate"], 15.0)

    def test_default_ledger_is_local_and_gitignored(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("NOOB_USAGE_DIR", None)
            ledger = UsageLedger(migrate=False)
        root = Path(noob_usage.__file__).resolve().parent
        self.assertEqual(ledger.directory, root / "usage")
        self.assertIn("usage/", (root / ".gitignore").read_text(encoding="utf-8"))

    def test_moves_the_legacy_ledger_once(self):
        with tempfile.TemporaryDirectory() as repo, tempfile.TemporaryDirectory() as home:
            legacy = Path(home) / "usage"
            old = UsageLedger(legacy)
            with old.transaction() as (state, events):
                events.append(codex_event("2026-10-28", cost=1.0))
            destination = Path(repo) / "usage"
            with mock.patch.object(noob_usage, "default_directory",
                                   return_value=destination), \
                 mock.patch.object(noob_usage, "legacy_directory", return_value=legacy):
                ledger = UsageLedger()
                again = UsageLedger()
            self.assertEqual(ledger.directory, destination)
            self.assertTrue(ledger.has_events())
            self.assertEqual(len(ledger.read_events()), 1)
            self.assertFalse(legacy.exists())
            self.assertEqual(len(again.read_events()), 1)


class CodexCaptureTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(self.root, ignore_errors=True))
        self.sessions = self.root / "sessions" / "2026" / "10" / "28"
        self.sessions.mkdir(parents=True)
        self.log = self.sessions / "rollout-2026-10-28T10-00-00-test.jsonl"
        self.write_log([
            ("2026-10-28T10:00:00Z", "turn_context", None),
            ("2026-10-28T10:01:00Z", "token_count", (100000, 50000, 10000)),
            ("2026-10-28T10:02:00Z", "token_count", (300000, 100000, 30000)),
        ])

    def write_log(self, entries):
        with self.log.open("w", encoding="utf-8") as stream:
            for stamp, kind, tokens in entries:
                if kind == "turn_context":
                    entry = {"timestamp": stamp, "type": "turn_context",
                             "payload": {"model": "gpt-6.1-sol"}}
                else:
                    input_tokens, cached, output = tokens
                    entry = {"timestamp": stamp, "type": "event_msg", "payload": {
                        "type": "token_count", "info": {"total_token_usage": {
                            "input_tokens": input_tokens,
                            "cached_input_tokens": cached,
                            "cache_write_input_tokens": 0,
                            "output_tokens": output}}}}
                stream.write(json.dumps(entry) + "\n")

    def append_log(self, entries):
        with self.log.open("a", encoding="utf-8") as stream:
            for stamp, tokens in entries:
                input_tokens, cached, output = tokens
                entry = {"timestamp": stamp, "type": "event_msg", "payload": {
                    "type": "token_count", "info": {"total_token_usage": {
                        "input_tokens": input_tokens,
                        "cached_input_tokens": cached,
                        "cache_write_input_tokens": 0,
                        "output_tokens": output}}}}
                stream.write(json.dumps(entry) + "\n")

    def capture(self, ledger):
        with mock.patch.dict(os.environ, {"CODEX_HOME": str(self.root)}), \
             mock.patch.object(noob, "capture_pi_usage", return_value=([], [])), \
             mock.patch.object(noob, "capture_opencode_codex_usage", return_value=([], [])), \
             mock.patch.object(noob, "capture_opencode_usage", return_value=([], [])):
            return noob.capture_usage(ledger, context(), NOW)

    def test_appends_only_new_deltas_and_survives_deletion(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = UsageLedger(tmp)
            warnings, files = self.capture(ledger)
            self.assertEqual(warnings, [])
            self.assertEqual(files, 1)
            row = ledger.aggregate(NOW)["Codex"]["all"]["gpt-6.1-sol"]
            self.assertEqual((row["input"], row["cached"], row["output"]),
                             (300000, 100000, 30000))
            first_count = len(ledger.read_events())

            warnings, _ = self.capture(ledger)
            self.assertEqual(len(ledger.read_events()), first_count)

            self.append_log([("2026-10-28T10:03:00Z", (400000, 100000, 40000))])
            warnings, _ = self.capture(ledger)
            row = ledger.aggregate(NOW)["Codex"]["all"]["gpt-6.1-sol"]
            self.assertEqual((row["input"], row["output"]), (400000, 40000))

            self.log.unlink()
            warnings, files = self.capture(ledger)
            self.assertEqual(files, 0)
            row = ledger.aggregate(NOW)["Codex"]["all"]["gpt-6.1-sol"]
            self.assertEqual((row["input"], row["output"]), (400000, 40000))

    def test_price_change_does_not_rewrite_old_events(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = UsageLedger(tmp)
            with mock.patch.object(noob, "RATES", {
                    "gpt-6.1-sol": (2.0, 0.1, 2.5, 10.0)}):
                self.capture(ledger)
            old_cost = sum(event["cost"] for event in ledger.read_events())
            self.append_log([
                ("2026-10-29T10:03:00Z", (500000, 100000, 50000)),
            ])
            with mock.patch.object(noob, "RATES", {
                    "gpt-6.1-sol": (20.0, 1.0, 25.0, 100.0)}):
                self.capture(ledger)
            events = ledger.read_events()
            first = [event for event in events if event["day"] == "2026-10-28"]
            latest = [event for event in events if event["day"] == "2026-10-29"]
            self.assertEqual(sum(event["cost"] for event in first), old_cost)
            self.assertGreater(sum(event["cost"] for event in latest), old_cost)

    def test_import_baseline_skips_existing_logs(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = UsageLedger(tmp)
            state = empty_state()
            state["baseline_only"] = True
            with mock.patch.dict(os.environ, {"CODEX_HOME": str(self.root)}):
                events, warnings, files = noob.capture_codex_usage(
                    state, NOW, context(), backfill=False, baseline=True)
            self.assertEqual(events, [])
            self.assertEqual(files, 1)
            self.assertTrue(state["codex"]["sessions"])


class DeepSeekCaptureTest(unittest.TestCase):
    def test_pi_messages_append_once(self):
        with tempfile.TemporaryDirectory() as sessions, tempfile.TemporaryDirectory() as tmp, \
             tempfile.TemporaryDirectory() as home:
            root = Path(sessions)
            log = root / "pi-session.jsonl"
            entry = {"timestamp": "2026-10-29T10:00:00Z", "type": "message",
                     "message": {"provider": "deepseek", "model": "deepseek-flash",
                                 "usage": {"input": 1000, "cacheRead": 200,
                                           "cacheWrite": 0, "output": 100}}}
            log.write_text(json.dumps(entry) + "\n", encoding="utf-8")
            ledger = UsageLedger(tmp)
            with mock.patch.dict(os.environ, {"PI_CODING_AGENT_SESSION_DIR": str(root),
                                              "CODEX_HOME": home}), \
                 mock.patch.object(noob, "capture_opencode_usage",
                                   return_value=([], [])):
                warnings, _ = noob.capture_usage(ledger, context(), NOW)
                self.assertEqual(warnings, [])
                count = len(ledger.read_events())
                warnings, _ = noob.capture_usage(ledger, context(), NOW)
                self.assertEqual(len(ledger.read_events()), count)
                with log.open("a", encoding="utf-8") as stream:
                    stream.write(json.dumps(entry) + "\n")
                warnings, _ = noob.capture_usage(ledger, context(), NOW)
            row = ledger.aggregate(NOW)["DeepSeek"]["day"]["deepseek-flash"]
            self.assertEqual(row["input"], 2400)
            self.assertEqual(row["output"], 200)
            self.assertEqual(row["estimate_cny"] > 0, True)
            self.assertEqual(ledger.period_rows("DeepSeek", NOW, source="pi")["day"]
                             ["deepseek-flash"]["input"], 2400)

    def test_opencode_daily_high_water_never_double_counts(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = UsageLedger(tmp)
            day = "2026-10-29"

            def usage(input_tokens):
                return {"deepseek-flash": {
                    "peak": noob.empty_local_row(),
                    "offpeak": {"input": input_tokens, "cached": 100, "write": 0,
                                "output": 50, "estimate": 0.0, "unpriced": 0,
                                "calls": 1}}}

            with mock.patch.object(noob.shutil, "which", return_value="/usr/bin/opencode"), \
                 mock.patch.object(noob, "opencode_stats", return_value={"activity": [
                     {"date": day, "steps": 1}]}) as stats, \
                 mock.patch.object(noob, "opencode_day_usage",
                                   side_effect=lambda start, end: usage(1000)) as day_usage:
                state = empty_state()
                events, warnings = noob.capture_opencode_usage(
                    state, NOW, context(), backfill=True, baseline=False)
                self.assertEqual(warnings, [])
                self.assertTrue(events)
                booked = state["opencode"]["days"][day]
                self.assertTrue(booked)
                day_usage.side_effect = lambda start, end: usage(1500)
                more, warnings = noob.capture_opencode_usage(
                    state, NOW, context(), backfill=False, baseline=False)
                self.assertEqual(sum(event["tokens"]["input"] for event in more), 500)
                day_usage.side_effect = lambda start, end: usage(200)
                less, _ = noob.capture_opencode_usage(
                    state, NOW, context(), backfill=False, baseline=False)
                self.assertEqual(less, [])
                self.assertEqual(state["opencode"]["days"][day][
                    "deepseek-flash|deepseek-flash|offpeak"]["input"], 1500)
            stats.assert_called()

    def test_opencode_codex_backfills_existing_ledger_without_repricing(self):
        now = NOW.astimezone()
        today = now.date().isoformat()
        yesterday = (now.date() - dt.timedelta(days=1)).isoformat()
        midnight = int(now.replace(hour=0, minute=0, second=0).timestamp() * 1000)
        usage = 500_000
        model = "gpt-6.2-sol"

        def stats(start, end):
            if start == 0:
                return {"activity": [{"date": yesterday}, {"date": today}]}
            if end < midnight:
                return {}
            return {"models": [
                {"model": {"id": model, "providerID": "openai", "variant": "high"},
                 "tokens": {"input": usage, "output": 100, "reasoning": 50,
                            "cache": {"read": 20, "write": 5}}, "steps": 2},
                {"model": {"id": model, "providerID": "openai", "variant": "low"},
                 "tokens": {"input": 50}, "steps": 1},
                {"model": {"id": model, "providerID": "deepseek"},
                 "tokens": {"input": 100}},
                {"model": {"id": "codex-auto-review", "providerID": "openai"},
                 "tokens": {"input": 100}},
            ]}

        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.object(noob.shutil, "which", return_value="/usr/bin/opencode"), \
             mock.patch.object(noob, "capture_codex_usage", return_value=([], [], 0)), \
             mock.patch.object(noob, "capture_pi_usage", return_value=([], [])), \
             mock.patch.object(noob, "capture_opencode_usage", return_value=([], [])), \
             mock.patch.object(noob, "opencode_stats", side_effect=stats) as query:
            ledger = UsageLedger(tmp)
            with ledger.transaction() as (state, events):
                events.append(codex_event(yesterday, model=model, cost=2.0))
                state["opencode"]["days"][yesterday] = {"existing-deepseek-entries": {}}
            self.assertEqual(noob.capture_usage(ledger, context(), now), ([], 0))
            original = ledger.read_events()
            imported = [event for event in original if event["source"] == "opencode"]
            self.assertEqual({event["day"] for event in imported}, {yesterday, today})
            self.assertTrue(all(event["backfill"] for event in imported))
            self.assertEqual(imported[-1]["tokens"], {
                "input": 500075, "cached": 20, "write": 5, "output": 150})
            self.assertEqual(imported[-1]["calls"], 3)
            self.assertAlmostEqual(imported[-1]["cost"], 1.0016145)
            self.assertEqual(ledger._load_state()["opencode"]["days"][yesterday],
                             {"existing-deepseek-entries": {}})
            query.reset_mock()
            noob.capture_usage(ledger, context(), now)
            self.assertEqual(ledger.read_events(), original)
            self.assertEqual(query.call_count, 1)
            self.assertEqual(query.call_args.args[0], midnight)
            usage = 100
            noob.capture_usage(ledger, context(), now)
            self.assertEqual(ledger.read_events(), original)
            usage = 510_000
            with mock.patch.dict(noob.RATES, {model: (4.0, 0.1, 2.5, 10.0)}):
                noob.capture_usage(ledger, context(), now)
            latest = ledger.read_events()
            self.assertEqual(latest[:-1], original)
            self.assertEqual(latest[-1]["tokens"]["input"], 10_000)
            self.assertAlmostEqual(latest[-1]["cost"], 0.04)
            self.assertFalse(latest[-1]["backfill"])

    def test_opencode_codex_uses_short_reference_rates_and_marks_missing_prices(self):
        panel = {"models": [
            {"model": {"id": "gpt-6.2-sol", "providerID": "openai"},
             "tokens": {"input": 500_000, "output": 100}},
            {"model": {"id": "unpriced-model", "providerID": "openai"},
             "tokens": {"input": 12, "cache": {"read": 3}, "reasoning": 5}},
        ]}
        with mock.patch.object(noob, "opencode_stats", return_value=panel):
            live = noob.opencode_codex_usage(NOW)
        self.assertAlmostEqual(live["all"]["gpt-6.2-sol"]["estimate"], 1.001)
        self.assertEqual(live["all"]["unpriced-model"]["unpriced"], 20)


class UiAdapterTest(unittest.TestCase):
    def test_codex_sources_split_on_home_and_merge_on_both_model_views(self):
        import noob_card
        import xml.etree.ElementTree as ET

        model = "gpt-6.2-sol"
        day = NOW.date().isoformat()
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.object(noob, "capture_usage", return_value=([], 2)):
            ledger = UsageLedger(tmp)
            with ledger.transaction() as (_, events):
                events.extend([codex_event(day, model=model, cost=1.25),
                               codex_event(day, model=model, cost=2.35, source="opencode"),
                               deepseek_event(day, cost_cny=8.00),
                               deepseek_event(day, source="pi", cost_cny=2.00)])
            usage = noob.collect_usage(context(), [], ledger=ledger, now=NOW)

        self.assertEqual([item["name"] for item in usage["platforms"]], ["Codex", "DeepSeek"])
        self.assertAlmostEqual(usage["models"][model]["day"]["cost"], 3.60)
        self.assertEqual(usage["models"][model]["day"]["tokens"], 2200)
        self.assertAlmostEqual(usage["totals"]["day"]["cost"], 3.60 + 10.00 / 7.1)
        for width in (32, 80, 120):
            home = noob.render_home(usage, {}, width=width)
            self.assertIn("Codex CLI", home)
            self.assertIn("OpenCode", home)
            self.assertIn("$3.60", home)
            self.assertIn("$1.25", home)
            self.assertIn("$2.35", home)
            self.assertLessEqual(max(map(len, home.splitlines())), width)
        models = noob.render_models(usage, width=120)
        self.assertEqual(models.count(model), 1)
        self.assertIn("$3.60", models)
        self.assertNotIn("Codex CLI", models)
        self.assertNotIn("OpenCode", models)
        card = ET.fromstring(noob_card.card_svg(usage))
        rows = list(card.find(".//*[@id='platform-rows']"))
        self.assertEqual([row.text for row in rows[::4]],
                         ["CODEX", "Codex CLI", "OpenCode", "DEEPSEEK", "opencode", "pi"])
        self.assertEqual([row.text for row in rows[1:12:4]], ["$3.60", "$1.25", "$2.35"])
        self.assertLessEqual(1244 + int(card.find(".//*[@id='card-footer']").get(
            "transform").split(",")[1][:-1]), 1332)
        models_card = noob_card.models_card_svg(usage)
        self.assertEqual(models_card.lower().count(model), 1)
        self.assertIn("$3.60", models_card)
        self.assertNotIn("Codex CLI", models_card)
        self.assertNotIn("OpenCode", models_card)

    def test_live_codex_totals_combine_both_clients(self):
        def source(cost):
            return {"day": {"gpt-6.2-sol": {"estimate": cost, "input": 100}}}

        with mock.patch.object(noob.shutil, "which", return_value="/usr/bin/opencode"), \
             mock.patch.object(noob, "scan_local_sessions", return_value=source(1.25)), \
             mock.patch.object(noob, "opencode_codex_usage", return_value=source(2.35)), \
             mock.patch.object(noob, "opencode_usage", return_value={}), \
             mock.patch.object(noob, "pi_usage", return_value={}):
            warnings = []
            usage = noob.collect_usage(context(), warnings, now=NOW)
        self.assertFalse(warnings)
        self.assertEqual(usage["platforms"][0]["currency"], "usd")
        self.assertEqual(usage["codex_split"]["codex"]["usd"]["day"], 1.25)
        self.assertEqual(usage["codex_split"]["opencode"]["usd"]["day"], 2.35)
        self.assertAlmostEqual(usage["models"]["gpt-6.2-sol"]["day"]["cost"], 3.60)

    def test_ledger_rows_keep_every_window_when_one_is_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = UsageLedger(tmp)
            with ledger.transaction() as (state, events):
                events.append(deepseek_event("2026-09-30", cost_cny=4.0))
            rows = ledger.period_rows("DeepSeek", NOW)
            self.assertEqual(sorted(rows), sorted(noob_usage.PERIODS))
            self.assertEqual(rows["day"], {})
            self.assertEqual(rows["all"]["DeepSeek V4.1 Flash"]["estimate_cny"], 4.0)
            sources = noob.ledger_usage_sources(ledger, NOW)
            self.assertEqual([name for name, _ in sources], ["DeepSeek"])

    def test_ledger_rows_feed_the_existing_dashboard(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = UsageLedger(tmp)
            with ledger.transaction() as (state, events):
                events.extend([
                    codex_event("2026-10-29", cost=1.0),
                    deepseek_event("2026-10-29", source="opencode", cost_cny=1.0),
                    deepseek_event("2026-10-29", source="pi", display="DeepSeek Flash",
                                   cost_cny=0.5),
                ])
            sources = noob.ledger_usage_sources(ledger, NOW, files=3)
            usage = noob.build_usage(sources)
            self.assertEqual(usage["files"], 3)
            self.assertEqual(usage["platforms"][0]["name"], "Codex")
            self.assertEqual(usage["platforms"][0]["totals"]["day"]["cost"], 1.0)
            self.assertEqual(usage["platforms"][1]["name"], "DeepSeek")
            opencode_rows = ledger.period_rows("DeepSeek", NOW, source="opencode")
            pi_rows = ledger.period_rows("DeepSeek", NOW, source="pi")
            split = noob.deepseek_split_costs(opencode_rows, pi_rows)
            self.assertEqual(split["opencode"]["cny"]["day"], 1.0)
            self.assertEqual(split["pi"]["cny"]["day"], 0.5)
            self.assertNotIn("MODELS", noob.render_home(usage, {}, width=80))


if __name__ == "__main__":
    unittest.main()
