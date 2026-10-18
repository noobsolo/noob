"""Read public Standard text prices, with an atomic last-good cache."""

import datetime as dt
import http.client
import json
import math
import os
import re
import tempfile
import urllib.request
from pathlib import Path


PRICING_URL = "https://developers.openai.com/api/docs/pricing.md"
CACHE_PATH = Path(__file__).with_name("pricing_cache.json")
MAX_PAGE_BYTES = 2_000_000
HEADERS = ("Model", "Short context input", "Short context cached input",
           "Short context cache writes", "Short context output", "Long context input",
           "Long context cached input", "Long context cache writes", "Long context output")


def price_cell(cell):
    if cell in ("-", "—"):
        return None
    if not re.fullmatch(r"\$\d+(?:\.\d+)?", cell):
        raise ValueError("Unexpected price format")
    value = float(cell[1:])
    if not math.isfinite(value):
        raise ValueError("Invalid price")
    return value


def parse_prices(markdown):
    """Only consume the explicitly labeled Standard text table."""
    section = re.search(r"^### Standard pricing data\s*\n(.*?)(?=^### |\Z)",
                        markdown, re.M | re.S)
    if not section:
        raise ValueError("Standard pricing table missing")
    lines = [line.strip() for line in section[1].splitlines() if line.strip().startswith("|")]
    cells = lambda line: tuple(cell.strip() for cell in line.strip("|").split("|"))
    if len(lines) < 3 or cells(lines[0]) != HEADERS:
        raise ValueError("Standard pricing columns changed")
    rates, long_rates = {}, {}
    for line in lines[2:]:
        row = cells(line)
        if len(row) != len(HEADERS):
            raise ValueError("Incomplete pricing row")
        model = re.fullmatch(r"([a-z0-9][a-z0-9.-]*)(?: \(<272K context length\))?", row[0])
        if not model:
            raise ValueError("Unexpected model name")
        model = model[1]
        if model in rates:
            raise ValueError("Duplicate model price")
        short, long = tuple(map(price_cell, row[1:5])), tuple(map(price_cell, row[5:9]))
        if short[0] is None or short[3] is None:
            raise ValueError("Input or output price missing")
        if any(value is not None for value in long):
            if long[0] is None or long[3] is None:
                raise ValueError("Incomplete long-context price")
            long_rates[model] = long
        rates[model] = short
    return rates, long_rates


def fetch_prices():
    request = urllib.request.Request(PRICING_URL, headers={"User-Agent": "noob/1.0"})
    with urllib.request.urlopen(request, timeout=8) as response:
        page = response.read(MAX_PAGE_BYTES + 1)
    if len(page) > MAX_PAGE_BYTES:
        raise ValueError("Pricing page too large")
    rates, long_rates = parse_prices(page.decode("utf-8"))
    return {"version": 1, "source": PRICING_URL,
            "fetched_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "rates": rates, "long_rates": long_rates}


def validate_snapshot(snapshot):
    if not isinstance(snapshot, dict) or snapshot.get("version") != 1 or snapshot.get("source") != PRICING_URL:
        raise ValueError("Invalid pricing cache")
    stamp = dt.datetime.fromisoformat(snapshot["fetched_at"])
    if stamp.tzinfo is None:
        raise ValueError("Missing cache timezone")
    result = dict(snapshot)
    for field in ("rates", "long_rates"):
        values = snapshot[field]
        if not isinstance(values, dict) or (field == "rates" and not values):
            raise ValueError("Invalid cached rates")
        result[field] = {}
        for model, row in values.items():
            if not re.fullmatch(r"[a-z0-9][a-z0-9.-]*", model) or not isinstance(row, (list, tuple)) or len(row) != 4:
                raise ValueError("Invalid cached model")
            for value in row:
                if value is not None and (type(value) not in (float, int) or not math.isfinite(value) or value < 0):
                    raise ValueError("Invalid cached price")
            if row[0] is None or row[3] is None:
                raise ValueError("Missing cached price")
            result[field][model] = tuple(row)
    if not result["long_rates"].keys() <= result["rates"].keys():
        raise ValueError("Unknown long-context model")
    return result


def save_snapshot(snapshot, path):
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=".noob-prices-", delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(snapshot, stream, indent=2, allow_nan=False)
            stream.write("\n")
        os.replace(temporary, path)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def refresh_prices(cache_path=None):
    path = Path(cache_path) if cache_path is not None else CACHE_PATH
    cached = None
    try:
        cached = validate_snapshot(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError, KeyError, TypeError):
        pass
    try:
        latest = validate_snapshot(fetch_prices())
    except (OSError, ValueError, KeyError, TypeError, http.client.HTTPException):
        if cached:
            return cached, "Price update unavailable; cached prices from %s." % cached["fetched_at"][:10]
        return None, "Price update unavailable; using built-in prices."
    # Preserve prices for retired models omitted from the latest page.
    if cached:
        latest["long_rates"] = {**{model: row for model, row in cached["long_rates"].items()
                                   if model not in latest["rates"]}, **latest["long_rates"]}
        latest["rates"] = {**cached["rates"], **latest["rates"]}
    try:
        save_snapshot(latest, path)
    except OSError:
        return latest, "Latest prices loaded; could not save the price cache."
    return latest, None
