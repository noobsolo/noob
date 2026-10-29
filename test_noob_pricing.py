import http.client
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import noob
import noob_pricing as pricing


TABLE = """# Pricing
### Standard pricing data
| Model | Short context input | Short context cached input | Short context cache writes | Short context output | Long context input | Long context cached input | Long context cache writes | Long context output |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| gpt-new-sol | $3.00 | $0.15 | $3.75 | $12.00 | $9.00 | $0.45 | $11.25 | $24.00 |
| gpt-5.5 (<272K context length) | $5.00 | $0.50 | - | $30.00 | $10.00 | $1.00 | - | $45.00 |
| gpt-new-pro | $30.00 | - | - | $180.00 | - | - | - | - |
### Batch pricing data
| Model | Short context input | Short context cached input | Short context cache writes | Short context output | Long context input | Long context cached input | Long context cache writes | Long context output |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| gpt-new-sol | $1.50 | $0.075 | $1.875 | $6.00 | $4.50 | $0.225 | $5.625 | $12.00 |
"""


NEW_TABLE = """# Pricing
### Standard pricing data
| Model | Short context input | Short context output | Short context cached input | Short context 30m write | Short context 12h write | Long context input | Long context output | Long context cached input | Long context 30m write | Long context 12h write |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| gpt-6.1-terra | $1.00 | $5.00 | $0.05 | $1.25 | $2.50 | $2.00 | $7.50 | $0.10 | $2.50 | $5.00 |
| gpt-5.5 (<272K context length) | $5.00 | $30.00 | $0.50 | - | - | $10.00 | $45.00 | $1.00 | - | - |

| Model | Input | Output | Cached input |
| --- | --- | --- | --- |
| gpt-4o | $2.50 | $10.00 | $1.25 |
| gpt-4o-mini | $0.15 | $0.60 | $0.075 |
"""


def snapshot():
    rates, long_rates = pricing.parse_prices(TABLE)
    return {"version": 1, "source": pricing.PRICING_URL,
            "fetched_at": "2026-10-09T12:00:00+00:00", "rates": rates, "long_rates": long_rates}


class PricingTest(unittest.TestCase):
    def test_new_ids_standard_only_and_explicit_long_rates(self):
        rates, long_rates = pricing.parse_prices(TABLE)
        self.assertEqual(rates["gpt-new-sol"], (3, .15, 3.75, 12))
        self.assertEqual(long_rates["gpt-new-sol"], (9, .45, 11.25, 24))
        self.assertIn("gpt-5.5", rates)
        self.assertEqual(rates["gpt-new-pro"], (30, None, None, 180))
        self.assertNotIn("gpt-new-pro", long_rates)

    def test_invalid_table_is_rejected(self):
        for page in ("<html>blocked</html>", TABLE.replace("Standard pricing data", "Fast pricing data"),
                     TABLE.replace("Short context input", "Changed column"),
                     TABLE.replace("$3.00", "$NaN"), TABLE.replace("$3.00", "-"),
                     TABLE.replace("$3.00 | $0.15", "$3.00")):
            with self.subTest(page=page[:70]), self.assertRaises(ValueError):
                pricing.parse_prices(page)

    def test_reordered_columns_and_split_tables_still_parse(self):
        rates, long_rates = pricing.parse_prices(NEW_TABLE)
        # New model family is picked up without a code change, 30m write is billed.
        self.assertEqual(rates["gpt-6.1-terra"], (1.0, 0.05, 1.25, 5.0))
        self.assertEqual(long_rates["gpt-6.1-terra"], (2.0, 0.1, 2.5, 7.5))
        # Cacheless rows keep their long-context rates and no write price.
        self.assertEqual(rates["gpt-5.5"], (5.0, 0.5, None, 30.0))
        self.assertEqual(long_rates["gpt-5.5"], (10.0, 1.0, None, 45.0))
        # The second, four-column table is priced without cache writes.
        self.assertEqual(rates["gpt-4o"], (2.5, 1.25, None, 10.0))
        self.assertEqual(rates["gpt-4o-mini"], (0.15, 0.075, None, 0.6))
        self.assertNotIn("gpt-4o", long_rates)

    def test_unrelated_tables_are_ignored_instead_of_failing(self):
        page = NEW_TABLE + """
| Model | Batch input | Batch output |
| --- | --- | --- |
| gpt-4o | $1.25 | $5.00 |
"""
        rates, _ = pricing.parse_prices(page)
        self.assertIn("gpt-6.1-terra", rates)
        self.assertEqual(rates["gpt-4o"], (2.5, 1.25, None, 10.0))

    def test_fetch_uses_public_page_and_bounded_timeout(self):
        with mock.patch.object(pricing.urllib.request, "urlopen") as open_page:
            open_page.return_value.__enter__.return_value.read.return_value = TABLE.encode()
            result = pricing.fetch_prices()
        request = open_page.call_args.args[0]
        self.assertEqual(request.full_url, pricing.PRICING_URL)
        self.assertNotIn("Authorization", request.headers)
        self.assertEqual(open_page.call_args.kwargs["timeout"], 8)
        self.assertIn("gpt-new-sol", result["rates"])

    def test_success_caches_and_network_failure_reuses_prices(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "prices.json"
            with mock.patch.object(pricing, "fetch_prices", return_value=snapshot()):
                latest, note = pricing.refresh_prices(path)
            self.assertIsNone(note)
            for error in (TimeoutError(), http.client.IncompleteRead(b"partial")):
                with mock.patch.object(pricing, "fetch_prices", side_effect=error):
                    cached, note = pricing.refresh_prices(path)
                self.assertEqual(cached, latest)
                self.assertIn("cached prices from 2026-10-09", note)
            self.assertEqual(list(Path(folder).iterdir()), [path])

    def test_broken_page_preserves_cache_and_invalid_cache_falls_back(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "prices.json"
            pricing.save_snapshot(snapshot(), path)
            original = path.read_bytes()
            with mock.patch.object(pricing, "fetch_prices", side_effect=ValueError):
                cached, note = pricing.refresh_prices(path)
            self.assertIsNotNone(cached)
            self.assertEqual(path.read_bytes(), original)
            for contents in ("broken", json.dumps({**snapshot(), "rates": {"bad": [-1, 0, 0, 1]}})):
                path.write_text(contents)
                with mock.patch.object(pricing, "fetch_prices", side_effect=TimeoutError):
                    cached, note = pricing.refresh_prices(path)
                self.assertIsNone(cached)
                self.assertIn("built-in", note)

    def test_retired_models_retained_and_fresh_prices_override_cache(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "prices.json"
            old = snapshot()
            old["rates"]["gpt-retired"] = (1, .1, 1.25, 5)
            old["long_rates"]["gpt-retired"] = (2, .2, 2.5, 7.5)
            old["rates"]["gpt-new-sol"] = (99, 9, 99, 99)
            old["long_rates"]["gpt-new-pro"] = (60, None, None, 270)
            pricing.save_snapshot(old, path)
            with mock.patch.object(pricing, "fetch_prices", return_value=snapshot()):
                latest, _ = pricing.refresh_prices(path)
            self.assertIn("gpt-retired", latest["long_rates"])
            self.assertEqual(latest["rates"]["gpt-new-sol"], (3, .15, 3.75, 12))
            self.assertNotIn("gpt-new-pro", latest["long_rates"])

    def test_cache_write_failure_keeps_live_prices(self):
        with mock.patch.object(pricing, "fetch_prices", return_value=snapshot()), \
             mock.patch.object(pricing, "save_snapshot", side_effect=PermissionError):
            latest, note = pricing.refresh_prices("/tmp/noob-test-missing-cache.json")
        self.assertIn("gpt-new-sol", latest["rates"])
        self.assertIn("could not save", note)

    def test_startup_loads_new_model_before_scan_and_uses_long_price(self):
        row = {"input": 300_000, "cached": 100_000, "write": 20_000, "output": 10_000}
        def scan():
            self.assertAlmostEqual(noob.estimate("gpt-new-sol", row), .75)
            self.assertAlmostEqual(noob.estimate("gpt-new-sol", row, True), 2.13)
            self.assertIsNone(noob.estimate("gpt-new-pro", row))
            result = {period: {} for period in ("day", "week", "month", "all")}
            for period in result:
                result[period]["gpt-new-sol"] = noob.empty_local_row()
                noob.add_local_usage(result[period], "gpt-new-sol", row)
            return result
        terminal = io.StringIO()
        with mock.patch.object(noob, "RATES", dict(noob.RATES)), \
             mock.patch.object(noob, "LONG_RATES", {}), \
             mock.patch.object(noob, "LONG_CONTEXT_MODELS", set(noob.LONG_CONTEXT_MODELS)), \
             mock.patch.object(noob, "refresh_prices", return_value=(snapshot(), None)) as refresh, \
             mock.patch.object(noob, "deepseek_api_key", return_value=None), \
             mock.patch.object(noob, "fetch_usd_cny", return_value=7.1), \
             mock.patch.object(noob, "deepseek_prices",
                               return_value=dict(noob.FALLBACK_DEEPSEEK_RATES)), \
             mock.patch.object(noob, "codex_data", return_value={}), \
             mock.patch.object(noob, "opencode_usage", return_value={}), \
             mock.patch.object(noob, "scan_local_sessions", side_effect=scan), \
             mock.patch.object(noob.sys, "stdout", terminal):
            self.assertEqual(noob.main(["--once"]), 0)
        refresh.assert_called_once_with()
        self.assertIn("$2.13", terminal.getvalue())
        self.assertNotIn("n/a", terminal.getvalue())


if __name__ == "__main__":
    unittest.main()
