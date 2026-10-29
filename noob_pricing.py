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
# Column names the page has used for each price, most specific first. Columns are
# looked up by name, so a new model family or a reordered table still parses.
SHORT_COLUMNS = {
    "input": ("Short context input", "Input"),
    "cached": ("Short context cached input", "Cached input"),
    "write": ("Short context 30m write", "Short context cache writes"),
    "output": ("Short context output", "Output"),
}
LONG_COLUMNS = {
    "input": ("Long context input",),
    "cached": ("Long context cached input",),
    "write": ("Long context 30m write", "Long context cache writes"),
    "output": ("Long context output",),
}
MODEL_CELL = re.compile(r"([a-z0-9][a-z0-9.-]*)(?: \(<272K context length\))?")


def price_cell(cell):
    if cell in ("-", "—"):
        return None
    if not re.fullmatch(r"\$\d+(?:\.\d+)?", cell):
        raise ValueError("Unexpected price format")
    value = float(cell[1:])
    if not math.isfinite(value):
        raise ValueError("Invalid price")
    return value


def markdown_tables(text):
    """Split markdown into tables of stripped cells (one table per block)."""
    tables, rows = [], []
    for line in text.splitlines():
        if line.strip().startswith("|"):
            rows.append([cell.strip() for cell in line.strip().strip("|").split("|")])
        elif rows:
            tables.append(rows)
            rows = []
    if rows:
        tables.append(rows)
    return tables


def column_indexes(headers, columns):
    """Map price fields to column positions, or None when the table is not one of ours."""
    positions = {name.lower(): index for index, name in enumerate(headers)}
    found = {field: next((positions[name.lower()] for name in candidates
                          if name.lower() in positions), None)
             for field, candidates in columns.items()}
    if found["input"] is None or found["output"] is None:
        return None
    return found


def row_prices(row, columns):
    """The (input, cached, write, output) cells of one row, in noob's order."""
    return tuple(price_cell(row[columns[field]]) if columns.get(field) is not None else None
                 for field in ("input", "cached", "write", "output"))


def parse_prices(markdown):
    """Read the Standard pricing tables, keyed by model id.

    The page spreads models over several tables and renames columns as pricing
    changes (the single cache-write column became 30m/12h tiers, output moved
    next to input, cacheless models got their own table), so every column is
    matched by name and any table without the columns we need is ignored. New
    models therefore appear without a code change.
    """
    section = re.search(r"^### Standard pricing data\s*\n(.*?)(?=^### |\Z)",
                        markdown, re.M | re.S)
    if not section:
        raise ValueError("Standard pricing table missing")
    rates, long_rates = {}, {}
    for table in markdown_tables(section[1]):
        short_columns = column_indexes(table[0], SHORT_COLUMNS)
        if short_columns is None:
            continue
        long_columns = column_indexes(table[0], LONG_COLUMNS)
        for row in table[1:]:
            model = MODEL_CELL.fullmatch(row[0])
            if not model:
                continue  # Separator, footnote, or a label we do not price.
            if len(row) != len(table[0]):
                raise ValueError("Incomplete pricing row")
            name = model[1]
            if name in rates:
                raise ValueError("Duplicate model price")
            short = row_prices(row, short_columns)
            if short[0] is None or short[3] is None:
                raise ValueError("Input or output price missing")
            long = row_prices(row, long_columns) if long_columns else (None,) * 4
            if any(value is not None for value in long):
                if long[0] is None or long[3] is None:
                    raise ValueError("Incomplete long-context price")
                long_rates[name] = long
            rates[name] = short
    if not rates:
        raise ValueError("Standard pricing columns changed")
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
