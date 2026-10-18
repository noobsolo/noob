#!/usr/bin/env python3
"""Subscription quotas, API balances, and local API-price-equivalent usage at a glance."""

import argparse
import datetime as dt
import json
import math
import os
import re
import select
import shutil
import subprocess
import sys
import textwrap
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from contextlib import contextmanager
from pathlib import Path

import termios
import tty

from shape_bar_demos import SHAPE, render_shape
from noob_card import save_card, save_models_card
from noob_reset_card import save_reset_card
from noob_pricing import refresh_prices


API_BASE = "https://api.openai.com/v1"
# Standard, short-context text rates in USD / million tokens, checked 2026-09-23.
# GPT-6.1 Sol added from its official model page on 2026-10-09.
# These are reference rates, never treated as actual invoice amounts.
RATES = {
    "gpt-6-astra": (10.0, 1.0, 12.5, 50.0),
    "gpt-6-sol": (2.0, 0.2, 2.5, 10.0),
    "gpt-6.1-sol": (2.0, 0.1, 2.5, 10.0),
    "gpt-6-luna": (0.1, 0.01, 0.125, 0.5),
    "gpt-5.6-sol": (4.0, 0.4, 5.0, 20.0),
    "gpt-5.6-terra": (2.0, 0.2, 2.5, 12.0),
    "gpt-5.6-luna": (0.2, 0.02, 0.25, 1.2),
    "gpt-5.5": (5.0, 0.5, None, 30.0),
    "gpt-5-codex": (1.25, 0.125, None, 10.0),
}
LONG_CONTEXT_MODELS = {"gpt-6-astra", "gpt-6-sol", "gpt-6.1-sol", "gpt-6-luna",
                       "gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna", "gpt-5.5"}
LONG_RATES = {}
TOKEN_FIELDS = ("input", "cached", "write", "output")
LOG_FIELDS = {"input": "input_tokens", "cached": "cached_input_tokens",
              "write": "cache_write_input_tokens", "output": "output_tokens"}
PACE_MARKER = "↑"
PACE_MARKER_COLOR = "1;38;2;46;139;87"
# Official site theme colors: OpenAI/ChatGPT green and DeepSeek blue.
PLATFORM_COLORS = {"CODEX": "1;38;2;16;163;127", "DEEPSEEK": "1;38;2;77;107;254"}
PLATFORM_ICONS = {"CODEX": "◆", "DEEPSEEK": "●"}
SUBSCRIPTION_INFO_PATH = Path(__file__).with_name("subscription_info.json")
DEEPSEEK_BALANCE_URL = "https://api.deepseek.com/user/balance"
DEEPSEEK_MODELS_URL = "https://api.deepseek.com/models"
DEEPSEEK_MODELS_PATH = Path(__file__).with_name("deepseek_models.json")
DEEPSEEK_PRICING_URL = "https://api-docs.deepseek.com/zh-cn/quick_start/pricing"
MAX_PRICING_BYTES = 600_000
# Per 1M tokens in CNY as (off-peak, peak); replaced by the live table at launch.
FALLBACK_DEEPSEEK_RATES = {
    "deepseek-flash": {"cache_hit": (0.02, 0.04), "cache_miss": (1.0, 2.0), "output": (4.0, 8.0)},
    "deepseek-v4-pro": {"cache_hit": (0.15, 0.30), "cache_miss": (4.5, 9.0), "output": (13.5, 27.0)},
}
# Retired DeepSeek ids that are still billed at the current Flash price.
DEEPSEEK_ALIASES = {"deepseek-v4-flash": "deepseek-flash",
                    "deepseek-v4-flash-vision-exp": "deepseek-flash"}
EXCHANGE_RATE_URL = "https://api.frankfurter.app/latest?from=USD&to=CNY"
# Used when the live rate cannot be fetched: how many CNY one USD buys.
FALLBACK_USD_CNY = 7.1
# DeepSeek bills double during these UTC hours on weekdays (Mon-Fri).
DEEPSEEK_PEAK_UTC = ((1, 4), (6, 10))
DEEPSEEK_PEAK_WEEKDAYS = (0, 1, 2, 3, 4)
MAX_PEAK_QUERIES = 60
KEYCHAIN_SERVICE = "noob-deepseek"
PERIODS = (("day", "TODAY"), ("week", "WEEK"), ("month", "MONTH"), ("all", "ALL"))


def rpc(proc, request_id, method, timeout=12):
    proc.stdin.write(json.dumps({"id": request_id, "method": method}) + "\n")
    proc.stdin.flush()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        ready, _, _ = select.select([proc.stdout], [], [], max(0, deadline - time.monotonic()))
        if not ready:
            break
        line = proc.stdout.readline()
        if not line:
            raise RuntimeError("Codex app-server exited")
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            continue
        if message.get("id") == request_id:
            if "error" in message:
                raise RuntimeError(str(message["error"].get("message", message["error"])))
            return message.get("result", {})
    raise RuntimeError("Codex app-server timed out")


def codex_data():
    proc = subprocess.Popen(
        ["codex", "app-server"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL, text=True, bufsize=1,
    )
    try:
        proc.stdin.write(json.dumps({"id": 1, "method": "initialize", "params": {
            "clientInfo": {"name": "noob", "title": "noob", "version": "1.0.0"}
        }}) + "\n")
        proc.stdin.flush()
        # Complete the handshake before any account request.
        deadline = time.monotonic() + 12
        while True:
            ready, _, _ = select.select([proc.stdout], [], [], max(0, deadline - time.monotonic()))
            if not ready:
                raise RuntimeError("Codex initialization timed out")
            line = proc.stdout.readline()
            if not line:
                raise RuntimeError("Codex app-server exited")
            response = json.loads(line)
            if response.get("id") == 1:
                if "error" in response:
                    raise RuntimeError(str(response["error"].get("message", response["error"])))
                break
        proc.stdin.write('{"method":"initialized","params":{}}\n')
        proc.stdin.flush()
        return rpc(proc, 2, "account/rateLimits/read")
    finally:
        if proc.poll() is None:
            proc.terminate()
        try:
            proc.communicate(timeout=2)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()


def api_pages(path, key, params):
    page = None
    seen = set()
    while True:
        query = dict(params)
        if page:
            query["page"] = page
        url = API_BASE + path + "?" + urllib.parse.urlencode(query)
        request = urllib.request.Request(url, headers={"Authorization": "Bearer " + key})
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                payload = json.load(response)
        except urllib.error.HTTPError as exc:
            raise RuntimeError("API request failed (HTTP %s); check the admin key and organization access" % exc.code) from None
        except (urllib.error.URLError, TimeoutError) as exc:
            raise RuntimeError("API connection failed: %s" % exc) from None
        yield from payload.get("data", [])
        page = payload.get("next_page")
        if not payload.get("has_more") or not page:
            break
        if page in seen:
            raise RuntimeError("API pagination cursor repeated")
        seen.add(page)


def api_data(key, now=None):
    now = now or dt.datetime.now(dt.timezone.utc).astimezone()
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    params = {"start_time": int(month_start.timestamp()), "end_time": int(now.timestamp()) + 1,
              "bucket_width": "1d", "limit": 31}
    costs = list(api_pages("/organization/costs", key, params))
    usage = list(api_pages("/organization/usage/completions", key, {**params, "group_by": "model"}))
    return month_start, costs, usage


def keychain_secret(service, timeout=5):
    """Read a generic password from the macOS Keychain, or None if unavailable."""
    if sys.platform != "darwin":
        return None
    try:
        result = subprocess.run(["security", "find-generic-password", "-s", service, "-w"],
                                capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def deepseek_api_key(environ=None):
    """Resolve the DeepSeek key from DEEPSEEK_API_KEY, then the macOS Keychain."""
    environ = os.environ if environ is None else environ
    key = environ.get("DEEPSEEK_API_KEY")
    return key or keychain_secret(KEYCHAIN_SERVICE)


def deepseek_balance(key, timeout=10):
    """Return the DeepSeek account balance as a list of per-currency entries."""
    request = urllib.request.Request(
        DEEPSEEK_BALANCE_URL,
        headers={"Authorization": "Bearer " + key, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.load(response)
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            raise RuntimeError("DeepSeek rejected the API key (HTTP %s)" % exc.code) from None
        raise RuntimeError("DeepSeek request failed (HTTP %s)" % exc.code) from None
    except (urllib.error.URLError, TimeoutError) as exc:
        raise RuntimeError("DeepSeek connection failed: %s" % exc) from None
    except (ValueError, OSError) as exc:
        raise RuntimeError("DeepSeek response was unreadable: %s" % exc) from None
    try:
        infos = payload["balance_infos"]
        if not isinstance(infos, list):
            raise TypeError
        entries = []
        for info in infos:
            entry = {"currency": info["currency"],
                     "total": float(info["total_balance"]),
                     "granted": float(info["granted_balance"] or 0),
                     "topped_up": float(info["topped_up_balance"] or 0)}
            if not all(math.isfinite(entry[field]) for field in ("total", "granted", "topped_up")):
                raise ValueError
            entries.append(entry)
    except (KeyError, TypeError, ValueError):
        raise RuntimeError("DeepSeek returned an unexpected balance payload") from None
    return entries


def fetch_usd_cny(timeout=6):
    """Return how many CNY one USD buys, or None when the rate is unavailable."""
    request = urllib.request.Request(EXCHANGE_RATE_URL, headers={"User-Agent": "noob/1.0"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.load(response)
        rate = float(payload["rates"]["CNY"])
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if not math.isfinite(rate) or rate <= 0:
        return None
    return rate


def parse_deepseek_prices(html):
    """Parse the DeepSeek pricing table into {model: {metric: (off, peak)}} CNY/1M."""
    table = re.search(r"<table.*?</table>", html, re.S)
    if not table:
        raise ValueError("DeepSeek pricing table missing")
    body = table.group(0)
    models = list(dict.fromkeys(re.findall(r"deepseek-[a-z0-9.\-]+", body)))
    if not models:
        raise ValueError("DeepSeek pricing models missing")
    metric = None
    rates = {model: {} for model in models}
    for row in re.findall(r"<tr>(.*?)</tr>", body, re.S):
        if "缓存命中" in row:
            metric = "cache_hit"
        elif "缓存未命中" in row:
            metric = "cache_miss"
        elif "输出" in row:
            metric = "output"
        if metric is None:
            continue
        band = 0 if "空闲" in row else 1 if "高峰" in row else None
        if band is None:
            continue
        values = [float(value) for value in re.findall(r"([0-9]+(?:\.[0-9]+)?)\s*元", row)]
        if len(values) != len(models):
            raise ValueError("Unexpected DeepSeek price row")
        for model, value in zip(models, values):
            entry = rates[model].setdefault(metric, [None, None])
            entry[band] = value
    for table_rates in rates.values():
        for name in ("cache_hit", "cache_miss", "output"):
            pair = table_rates.get(name)
            if not pair or pair[0] is None or pair[1] is None:
                raise ValueError("Incomplete DeepSeek price table")
            table_rates[name] = (pair[0], pair[1])
    return rates


def fetch_deepseek_prices(timeout=8):
    request = urllib.request.Request(DEEPSEEK_PRICING_URL, headers={"User-Agent": "noob/1.0"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        page = response.read(MAX_PRICING_BYTES + 1)
    if len(page) > MAX_PRICING_BYTES:
        raise ValueError("DeepSeek pricing page too large")
    return parse_deepseek_prices(page.decode("utf-8"))


def deepseek_prices():
    """Live DeepSeek table (CNY/1M), or the built-in fallback when it is unavailable."""
    try:
        return fetch_deepseek_prices()
    except (OSError, ValueError, UnicodeError):
        return {model: dict(rates) for model, rates in FALLBACK_DEEPSEEK_RATES.items()}


def deepseek_rate(rates, model):
    """Price row for a model id, or None when the table has no rate for it."""
    return rates.get(model) or rates.get(DEEPSEEK_ALIASES.get(model))


def deepseek_token_cost(table, miss, hit, output, peak):
    """CNY cost for one model's tokens; `peak` selects the peak column."""
    band = 1 if peak else 0
    return (miss * table["cache_miss"][band] + hit * table["cache_hit"][band]
            + output * table["output"][band]) / 1_000_000


def fetch_deepseek_models(key, timeout=8):
    """Return {model_id: display name} from DeepSeek's model list, or None."""
    if not key:
        return None
    request = urllib.request.Request(DEEPSEEK_MODELS_URL,
                                     headers={"Authorization": "Bearer " + key,
                                              "Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.load(response)
        models = {}
        for item in payload["data"]:
            model_id = item.get("id")
            if model_id:
                models[model_id] = item.get("name") or model_id
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return models or None


def load_deepseek_models():
    """{model_id: [{'name': ..., 'since': iso}]}, tolerating a missing/corrupt file."""
    try:
        data = json.loads(DEEPSEEK_MODELS_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    history = {}
    for model_id, entries in data.items():
        if not isinstance(entries, list):
            continue
        kept = [entry for entry in entries
                if isinstance(entry, dict) and isinstance(entry.get("name"), str)
                and isinstance(entry.get("since"), str)]
        if kept:
            history[model_id] = kept
    return history


def save_deepseek_models(history):
    temporary = DEEPSEEK_MODELS_PATH.with_name("." + DEEPSEEK_MODELS_PATH.name + ".tmp")
    try:
        temporary.write_text(json.dumps(history, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, DEEPSEEK_MODELS_PATH)
    except OSError:
        temporary.unlink(missing_ok=True)


def record_deepseek_versions(history, current, now=None):
    """Append newly observed model names; return True when the history changed."""
    if not current:
        return False
    now = now or dt.datetime.now().astimezone()
    changed = False
    for model_id, name in current.items():
        entries = history.setdefault(model_id, [])
        if not entries or entries[-1].get("name") != name:
            entries.append({"name": name, "since": now.isoformat()})
            changed = True
    return changed


def deepseek_version_at(versions, model_id, moment):
    """Display name of the version recorded at `moment`, or None."""
    name = None
    for entry in versions.get(model_id) or []:
        try:
            since = dt.datetime.fromisoformat(entry["since"])
        except (KeyError, TypeError, ValueError):
            continue
        if since <= moment:
            name = entry["name"]
    if name is None:
        entries = versions.get(model_id) or []
        if entries:
            name = entries[0]["name"]
    return name


def deepseek_version_spans(versions, model_id, start, end):
    """[(name, start, end)] splitting [start, end] by recorded version changes."""
    boundaries = set()
    entries = []
    for entry in versions.get(model_id) or []:
        try:
            since = dt.datetime.fromisoformat(entry["since"])
        except (KeyError, TypeError, ValueError):
            continue
        entries.append((since, entry["name"]))
    previous = None
    for since, name in sorted(entries, key=lambda item: item[0]):
        if previous is not None and name != previous and start < since < end:
            boundaries.add(since)
        previous = name
    spans = []
    cursor = start
    for boundary in sorted(boundaries) + [end]:
        spans.append((deepseek_version_at(versions, model_id, cursor) or model_id, cursor, boundary))
        cursor = boundary
    return spans


def quota_rows(payload):
    buckets = payload.get("rateLimitsByLimitId") or {}
    if not buckets and payload.get("rateLimits"):
        bucket = payload["rateLimits"]
        buckets = {bucket.get("limitId", "codex"): bucket}
    rows = []
    for key, bucket in buckets.items():
        for window_key, window in (("primary", bucket.get("primary")),
                                   ("secondary", bucket.get("secondary"))):
            if not window:
                continue
            minutes = window.get("windowDurationMins")
            if minutes is None:
                label = window_key
            elif minutes >= 1440 and minutes % 1440 == 0:
                label = "%dd" % (minutes // 1440)
            elif minutes >= 60 and minutes % 60 == 0:
                label = "%dh" % (minutes // 60)
            else:
                label = "%sm" % minutes
            rows.append((bucket.get("limitName") or key, label,
                         max(0.0, min(100.0, float(window.get("usedPercent") or 0))),
                         window.get("resetsAt"), minutes))
    return rows


def card_quotas(payload):
    """Return remaining quota and terminal color for the default Codex windows."""
    bucket = payload.get("rateLimits") or (payload.get("rateLimitsByLimitId") or {}).get("codex")
    if not bucket:
        return {}
    result = {}
    for _, _, used, _, minutes in quota_rows({"rateLimits": bucket}):
        if minutes in (300, 10080):
            remaining = round(100 - used)
            result[minutes] = (remaining, quota_tone(remaining))
    return result


def model_totals(buckets):
    totals = defaultdict(lambda: {"input": 0, "cached": 0, "write": 0, "output": 0, "requests": 0})
    for bucket in buckets:
        for item in bucket.get("results", []):
            row = totals[item.get("model") or "Unknown model"]
            for field, source in (("input", "input_tokens"), ("cached", "input_cached_tokens"),
                                  ("write", "input_cache_write_tokens"), ("output", "output_tokens"),
                                  ("requests", "num_model_requests")):
                row[field] += item.get(source) or 0
    return totals


def estimate(model, row, long_context=False):
    rates = RATES.get(model)
    if rates is None:
        return None
    explicit_long_rates = long_context and model in LONG_RATES
    if explicit_long_rates:
        rates = LONG_RATES[model]
    normal, cached, write, output = rates
    if (row["write"] and write is None) or (row["cached"] and cached is None):
        return None
    uncached = max(0, row["input"] - row["cached"] - row["write"])
    use_multiplier = long_context and model in LONG_CONTEXT_MODELS and not explicit_long_rates
    input_multiplier = 2 if use_multiplier else 1
    output_multiplier = 1.5 if use_multiplier else 1
    return (input_multiplier * (uncached * normal + row["cached"] * (cached or 0) +
            row["write"] * (write or 0)) + output_multiplier * row["output"] * output) / 1_000_000


def empty_local_row():
    return {"input": 0, "cached": 0, "write": 0, "output": 0, "estimate": 0.0,
            "unpriced": 0, "calls": 0}


def add_local_usage(rows, model, token_delta):
    row = rows[model]
    for field in TOKEN_FIELDS:
        row[field] += token_delta[field]
    row["calls"] += 1
    cost = estimate(model, token_delta, long_context=token_delta["input"] > 272_000)
    if cost is None:
        row["unpriced"] += token_delta["input"] + token_delta["output"]
    else:
        row["estimate"] += cost


def scan_local_sessions(root=None, now=None):
    root = Path(root or os.environ.get("CODEX_HOME", "~/.codex")).expanduser() / "sessions"
    now = now or dt.datetime.now().astimezone()
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    week_start = day_start - dt.timedelta(days=day_start.weekday())
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    rows = {period: defaultdict(empty_local_row) for period in ("day", "week", "month", "all")}
    file_count = 0
    for path in root.rglob("rollout-*.jsonl") if root.is_dir() else ():
        file_count += 1
        model = None
        previous = None
        try:
            with path.open("r", encoding="utf-8") as stream:
                for line in stream:
                    try:
                        entry = json.loads(line)
                    except json.JSONDecodeError:
                        continue  # An active Codex process may be writing the final line.
                    payload = entry.get("payload") or {}
                    if entry.get("type") == "turn_context":
                        model = payload.get("model") or model
                        continue
                    if entry.get("type") != "event_msg" or payload.get("type") != "token_count":
                        continue
                    total = (payload.get("info") or {}).get("total_token_usage")
                    if not isinstance(total, dict):
                        continue
                    current = {field: max(0, int(total.get(source) or 0))
                               for field, source in LOG_FIELDS.items()}
                    if previous is None:
                        delta = current
                    elif any(current[field] < previous[field] for field in TOKEN_FIELDS):
                        previous = current
                        continue
                    else:
                        delta = {field: current[field] - previous[field] for field in TOKEN_FIELDS}
                    previous = current
                    if not any(delta.values()):
                        continue
                    if model == "codex-auto-review":
                        continue
                    try:
                        stamp = dt.datetime.fromisoformat(entry["timestamp"].replace("Z", "+00:00")).astimezone()
                    except (KeyError, TypeError, ValueError):
                        continue
                    name = model or "Unknown model"
                    add_local_usage(rows["all"], name, delta)
                    if day_start <= stamp <= now:
                        add_local_usage(rows["day"], name, delta)
                    if week_start <= stamp <= now:
                        add_local_usage(rows["week"], name, delta)
                    if month_start <= stamp <= now:
                        add_local_usage(rows["month"], name, delta)
        except OSError:
            file_count -= 1
    return {**rows, "files": file_count, "day_start": day_start,
            "week_start": week_start, "month_start": month_start}


OPENCODE_STATS_PATH = "/api/experimental/session/stats"


def opencode_stats(from_ms, to_ms, timeout=20):
    """Read one range of OpenCode's local session statistics through the CLI."""
    command = ["opencode", "api", "get",
               "%s?from=%d&to=%d" % (OPENCODE_STATS_PATH, from_ms, to_ms)]
    result = subprocess.run(command, capture_output=True, text=True, timeout=timeout)
    if result.returncode != 0:
        raise RuntimeError((result.stderr or result.stdout or "opencode api failed").strip()[:160])
    try:
        data = json.loads(result.stdout)["data"]
    except (KeyError, TypeError, ValueError):
        raise RuntimeError("OpenCode returned an unexpected stats payload") from None
    if not isinstance(data, dict):
        raise RuntimeError("OpenCode returned an unexpected stats payload")
    return data


def peak_windows(start, end):
    """DeepSeek peak windows (UTC) clipped to [start, end], as (from_ms, to_ms)."""
    if start is None or start >= end:
        return []
    utc = dt.timezone.utc
    start_utc, end_utc = start.astimezone(utc), end.astimezone(utc)
    first_day = dt.datetime.combine(start_utc.date(), dt.time.min, tzinfo=utc)
    cursor = dt.datetime.combine(end_utc.date(), dt.time.min, tzinfo=utc)
    windows = []
    while cursor >= first_day and len(windows) < MAX_PEAK_QUERIES:
        if cursor.weekday() in DEEPSEEK_PEAK_WEEKDAYS:
            for hour_from, hour_to in DEEPSEEK_PEAK_UTC:
                low = max(cursor + dt.timedelta(hours=hour_from), start_utc)
                high = min(cursor + dt.timedelta(hours=hour_to), end_utc)
                if low < high:
                    windows.append((int(low.timestamp() * 1000), int(high.timestamp() * 1000)))
        cursor -= dt.timedelta(days=1)
    return windows


def opencode_deepseek_tokens(data):
    """Per-model DeepSeek token rows from one OpenCode stats payload."""
    bucket = defaultdict(empty_local_row)
    for entry in data.get("models") or []:
        model = entry.get("model") or {}
        if model.get("providerID") != "deepseek":
            continue
        tokens = entry.get("tokens") or {}
        cache = tokens.get("cache") or {}
        read = int(cache.get("read") or 0)
        write = int(cache.get("write") or 0)
        row = bucket[model.get("id") or "deepseek"]
        row["input"] += int(tokens.get("input") or 0) + read + write
        row["cached"] += read
        row["write"] += write
        row["output"] += int(tokens.get("output") or 0) + int(tokens.get("reasoning") or 0)
        row["calls"] += int(entry.get("steps") or 0)
    return dict(bucket)


def opencode_usage(now=None, timeout=20, rates=None, usd_cny=FALLBACK_USD_CNY, versions=None):
    """Return per-period rows for DeepSeek models used through OpenCode.

    Tokens come from OpenCode, but the cost is computed from DeepSeek's own price
    table (CNY) with weekday peak windows billed at double the off-peak rate, then
    converted to USD at `usd_cny`. When `versions` records a model's display-name
    history, usage is sliced at the recorded change times so each version becomes
    its own row.
    """
    rates = rates or FALLBACK_DEEPSEEK_RATES
    versions = versions or {}
    periods = ("day", "week", "month", "all")
    now = now or dt.datetime.now().astimezone()
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    week_start = day_start - dt.timedelta(days=day_start.weekday())
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    to_ms = int(now.timestamp() * 1000)
    starts = {"day": day_start, "week": week_start, "month": month_start, "all": None}
    base = {}
    earliest = None
    for period in periods:
        start = starts[period]
        from_ms = 0 if start is None else int(start.timestamp() * 1000)
        if from_ms >= to_ms:
            base[period] = {}
            continue
        data = opencode_stats(from_ms, to_ms, timeout)
        base[period] = opencode_deepseek_tokens(data)
        if period == "all":
            for item in data.get("activity") or []:
                stamp = item.get("date")
                if stamp and (earliest is None or stamp < earliest):
                    earliest = stamp
    if earliest:
        try:
            starts["all"] = dt.datetime.combine(dt.date.fromisoformat(earliest),
                                                dt.time.min).astimezone()
        except ValueError:
            pass

    # One version span list per (period, model); the first span comes from the
    # period total, later spans are queried individually.
    span_lists = {period: {} for period in periods}
    for period in periods:
        start = starts[period] if starts[period] is not None else now
        for model_id in base.get(period) or {}:
            span_lists[period][model_id] = deepseek_version_spans(versions, model_id, start, now)

    range_cache = {}

    def tokens_in(start, end):
        key = (int(start.timestamp() * 1000), int(end.timestamp() * 1000))
        if key not in range_cache:
            range_cache[key] = opencode_deepseek_tokens(
                opencode_stats(key[0], key[1], timeout))
        return range_cache[key]

    buckets = {period: {} for period in periods}
    for period in periods:
        for model_id, spans in span_lists[period].items():
            if len(spans) == 1:
                buckets[period][(model_id, 0)] = dict(base[period][model_id])
                continue
            remainder = {field: base[period][model_id][field]
                         for field in ("input", "cached", "write", "output", "calls")}
            for index, (_, span_start, span_end) in enumerate(spans[1:], start=1):
                rows = tokens_in(span_start, span_end).get(model_id) or empty_local_row()
                buckets[period][(model_id, index)] = {
                    field: rows[field] for field in ("input", "cached", "write", "output", "calls")}
                for field in remainder:
                    remainder[field] -= rows[field]
            buckets[period][(model_id, 0)] = remainder

    window_tokens = {}
    hot = {}
    for period in periods:
        if not base.get(period):
            continue
        start = starts[period] if starts[period] is not None else now
        for window in peak_windows(start, now):
            if window not in window_tokens:
                window_tokens[window] = opencode_deepseek_tokens(
                    opencode_stats(window[0], window[1], timeout))
            moment = dt.datetime.fromtimestamp(window[0] / 1000).astimezone()
            for model_id, rows in window_tokens[window].items():
                spans = span_lists[period].get(model_id)
                if not spans:
                    continue
                index = len(spans) - 1
                for position, (_, span_start, span_end) in enumerate(spans):
                    if span_start <= moment < span_end:
                        index = position
                        break
                target = hot.setdefault((period, model_id, index), empty_local_row())
                for field in ("input", "cached", "write", "output", "calls"):
                    target[field] += rows[field]

    rows = {}
    for period in periods:
        period_rows = {}
        for (model_id, index), token_row in buckets[period].items():
            spans = span_lists[period].get(model_id) or [(model_id, None, None)]
            name = spans[index][0] if index < len(spans) else model_id
            row = dict(token_row)
            table = deepseek_rate(rates, model_id)
            if table is None:
                row["unpriced"] = 1
                row["estimate"] = 0.0
                row["estimate_cny"] = 0.0
            else:
                peak_row = hot.get((period, model_id, index)) or empty_local_row()
                miss = row["input"] - row["cached"] - row["write"]
                peak_miss = peak_row["input"] - peak_row["cached"] - peak_row["write"]
                cost = (deepseek_token_cost(table, miss - peak_miss,
                                            row["cached"] - peak_row["cached"],
                                            row["output"] - peak_row["output"], False)
                        + deepseek_token_cost(table, peak_miss, peak_row["cached"],
                                              peak_row["output"], True))
                row["estimate"] = cost / usd_cny
                row["estimate_cny"] = cost
            target = period_rows.get(name)
            if target is None:
                period_rows[name] = row
            else:
                for field in ("input", "cached", "write", "output", "calls",
                              "unpriced", "estimate", "estimate_cny"):
                    target[field] = target.get(field, 0) + (row.get(field, 0) or 0)
        rows[period] = period_rows
    return rows


def pi_sessions_root():
    """Directory holding pi session JSONL files (default ~/.pi/agent/sessions)."""
    override = os.environ.get("PI_CODING_AGENT_SESSION_DIR")
    if override:
        return Path(override).expanduser()
    base = Path(os.environ.get("PI_CODING_AGENT_DIR", "~/.pi/agent")).expanduser()
    return base / "sessions"


def pi_usage(now=None, usd_cny=FALLBACK_USD_CNY, versions=None, rates=None):
    """Return per-period DeepSeek rows recorded by pi's own session logs.

    Tokens come from pi's per-message `usage` block (`reasoning` is already part
    of `output`, so it is not added again). The cost is NOT pi's own USD figure:
    pi prices every message at DeepSeek's standard rate, which equals the peak
    column, so off-peak usage would be billed at double. Instead each message is
    priced from DeepSeek's own CNY table, using its timestamp to pick the peak or
    off-peak column exactly, then converted to USD at `usd_cny`.
    """
    versions = versions or {}
    rates = rates or FALLBACK_DEEPSEEK_RATES
    periods = ("day", "week", "month", "all")
    now = now or dt.datetime.now().astimezone()
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    week_start = day_start - dt.timedelta(days=day_start.weekday())
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    rows = {period: defaultdict(empty_local_row) for period in periods}
    root = pi_sessions_root()
    if root.is_dir():
        for path in root.rglob("*.jsonl"):
            try:
                with path.open("r", encoding="utf-8") as stream:
                    for line in stream:
                        try:
                            entry = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        if entry.get("type") != "message":
                            continue
                        message = entry.get("message") or {}
                        if message.get("provider") != "deepseek":
                            continue
                        usage = message.get("usage") or {}
                        if not isinstance(usage, dict):
                            continue
                        try:
                            stamp = dt.datetime.fromisoformat(
                                entry["timestamp"].replace("Z", "+00:00")).astimezone()
                        except (KeyError, TypeError, ValueError):
                            continue
                        cache_read = max(0, int(usage.get("cacheRead") or 0))
                        cache_write = max(0, int(usage.get("cacheWrite") or 0))
                        input_tokens = max(0, int(usage.get("input") or 0))
                        output_tokens = max(0, int(usage.get("output") or 0))
                        if input_tokens + cache_read + cache_write + output_tokens <= 0:
                            continue
                        model_id = message.get("model") or "deepseek"
                        table = deepseek_rate(rates, model_id)
                        cost = None if table is None else deepseek_token_cost(
                            table, input_tokens, cache_read, output_tokens,
                            deepseek_peak(stamp))
                        name = deepseek_version_at(versions, model_id, stamp) or model_id
                        for period, start in (("day", day_start), ("week", week_start),
                                              ("month", month_start), ("all", None)):
                            if start is not None and not (start <= stamp <= now):
                                continue
                            row = rows[period][name]
                            row.setdefault("estimate_cny", 0.0)
                            row["input"] += input_tokens + cache_read + cache_write
                            row["cached"] += cache_read
                            row["write"] += cache_write
                            row["output"] += output_tokens
                            row["calls"] += 1
                            if cost is None:
                                row["unpriced"] += input_tokens + output_tokens
                            else:
                                row["estimate"] += cost / usd_cny
                                row["estimate_cny"] += cost
            except OSError:
                continue
    return {period: {name: dict(row) for name, row in rows[period].items()}
            for period in periods}


def deepseek_peak(moment):
    """True when `moment` falls in DeepSeek's weekday peak window (UTC)."""
    utc = moment.astimezone(dt.timezone.utc)
    return (utc.weekday() in DEEPSEEK_PEAK_WEEKDAYS
            and any(low <= utc.hour < high for low, high in DEEPSEEK_PEAK_UTC))


def merge_deepseek_usage(*sources):
    """Sum multiple {period: {model: row}} DeepSeek results into one."""
    periods = ("day", "week", "month", "all")
    merged = {period: {} for period in periods}
    for source in sources:
        for period in periods:
            for model, row in (source.get(period) or {}).items():
                target = merged[period].setdefault(model, dict(empty_local_row()))
                target.setdefault("estimate_cny", 0.0)
                for field in ("input", "cached", "write", "output", "calls",
                              "estimate", "estimate_cny", "unpriced"):
                    target[field] += row.get(field, 0) or 0
    return merged


def deepseek_split_costs(opencode_rows, pi_rows):
    """Per-period CNY cost for the opencode and pi DeepSeek sources separately."""
    result = {}
    for name, rows in (("opencode", opencode_rows), ("pi", pi_rows)):
        cny = {period: 0.0 for period, _ in PERIODS}
        for period, _ in PERIODS:
            for row in ((rows or {}).get(period) or {}).values():
                cny[period] += row.get("estimate_cny", 0.0)
        result[name] = {"cny": cny}
    return result


def deepseek_split_lines(split, width, color, indent=4):
    """Dimmed opencode/pi cost sub-rows shown under the DEEPSEEK cost line."""
    if not split:
        return []
    lines = []
    for name in ("opencode", "pi"):
        cny = ((split.get(name) or {}).get("cny")) or {}
        pairs = [(label.lower(), "¥%.2f" % (cny.get(period) or 0.0))
                 for period, label in PERIODS]
        lines.extend(summary_lines(name, pairs, width, color, indent=indent,
                                   key_style="2", value_style="2", uppercase=False))
    return lines


def quota_tone(remaining):
    return "32" if remaining >= 60 else "33" if remaining >= 20 else "31"


def bar(remaining, width=24, color=False):
    return render_shape(SHAPE, remaining, width=width, color=color)


def pace_marker(window_minutes, resets_at, width, now_ts=None):
    if not window_minutes or not resets_at or width < 1:
        return None
    duration = float(window_minutes) * 60
    if duration <= 0:
        return None
    now_ts = time.time() if now_ts is None else now_ts
    fraction = max(0.0, min(1.0, (float(resets_at) - now_ts) / duration))
    return round((width - 1) * fraction)


def paint(value, style, color):
    return "\033[%sm%s\033[0m" % (style, value) if color else value


def compact_count(value):
    if value >= 1_000_000:
        return "%.2fM" % (value / 1_000_000)
    if value >= 1_000:
        return "%.1fK" % (value / 1_000)
    return str(value)


def clipped(value, width):
    return value if len(value) <= width else value[:max(0, width - 1)] + "…"


def when(timestamp):
    if not timestamp:
        return "unknown"
    target = dt.datetime.fromtimestamp(timestamp).astimezone()
    now = dt.datetime.now().astimezone()
    if target.date() == now.date():
        return target.strftime("%H:%M")
    return "%s %d %s" % (target.strftime("%b"), target.day, target.strftime("%H:%M"))


def load_subscription_info():
    try:
        with SUBSCRIPTION_INFO_PATH.open() as source:
            return json.load(source)
    except (OSError, ValueError):
        return None


def subscription_text(info, available=80, today=None):
    """Renewal summary sized for `available` columns, or None when undated."""
    if not isinstance(info, dict):
        return None
    today = dt.date.today() if today is None else today
    try:
        renewal = dt.date.fromisoformat(info["renewal_date"])
    except (KeyError, TypeError, ValueError):
        return None
    try:
        start = dt.date.fromisoformat(info["started_on"])
    except (KeyError, TypeError, ValueError):
        start = None
    days = (today - start).days if start and start <= today else None
    days_text = "%d" % days if days is not None else "—"
    if renewal >= today:
        label = "%s  ·  subscribed %s days" % (renewal.strftime("%b %d, %Y").lower(), days_text)
        if len(label) > available:
            label = "%s  ·  %s days" % (renewal.strftime("%m/%d/%y"), days_text)
        if len(label) > available:
            label = "%s  ·  %sd" % (renewal.strftime("%m/%d"), days_text)
    else:
        label = "date unverified  ·  subscribed %s days" % days_text
        if len(label) > available:
            label = "unverified  ·  %sd" % days_text
    return clipped(label, max(4, available))


def deepseek_money_text(balance, usd_cny=FALLBACK_USD_CNY):
    """DeepSeek balance in CNY; any non-CNY entry is converted at usd_cny."""
    if not balance:
        return None
    total = 0.0
    for entry in balance:
        value = float(entry["total"])
        if str(entry.get("currency", "CNY")).upper() != "CNY":
            value *= usd_cny
        total += value
    return "¥%.2f" % total


def local_summary(rows):
    return (sum(row["input"] + row["output"] for row in rows.values()),
            sum(row["estimate"] for row in rows.values()),
            sum(row["unpriced"] for row in rows.values()))


def money(value):
    return "<$0.01" if 0 < value < 0.005 else "$%.2f" % value


def countdown_line(seconds, width=80, color=False):
    label = "Refresh in %ds" % seconds
    detail = " · Ctrl-C to exit"
    if len(label) + len(detail) + 2 < width:
        label += detail
    return paint("  " + label, "2", color)


def reset_banner(credits, width=80, color=False):
    width = max(20, width)
    count = credits.get("availableCount") if isinstance(credits, dict) else None
    status = "%s AVAILABLE" % count if isinstance(count, int) and count >= 0 else "UNKNOWN"
    label = "★  CODEX RESET  ·  %s" % status
    if len(label) + 2 > width:
        label = "★  RESETS: %s" % (count if isinstance(count, int) and count >= 0 else "?")

    available = []
    if isinstance(credits, dict) and isinstance(credits.get("credits"), list):
        available = [item for item in credits["credits"]
                     if isinstance(item, dict) and item.get("status") == "available"]
    available.sort(key=lambda item: item.get("expiresAt")
                   if isinstance(item.get("expiresAt"), (int, float))
                   and not isinstance(item.get("expiresAt"), bool) else float("inf"))
    content_width = width - 2
    lines = [label]
    if available:
        if width < 68:
            lines.append("EXPIRES")
        elif len(label) + len("EXPIRES") <= content_width:
            lines[0] = label.ljust(content_width - len("EXPIRES")) + "EXPIRES"
        else:
            lines.append("EXPIRES")

        details = []
        for index, item in enumerate(available, 1):
            marker = "①②③④⑤⑥⑦⑧⑨⑩"[index - 1] if index <= 10 else "#%s" % index
            expires = item.get("expiresAt")
            timestamp = (dt.datetime.fromtimestamp(expires).strftime("%b %d %H:%M")
                         if isinstance(expires, (int, float)) and not isinstance(expires, bool)
                         else "time unknown")
            details.append("%s %s" % (marker, timestamp))
        if width < 68:
            lines.extend(details)
        else:
            row = ""
            for detail in details:
                candidate = row + ("  │  " if row else "") + detail
                if len(candidate) > content_width and row:
                    lines.append(row)
                    row = detail
                else:
                    row = candidate
            if row:
                lines.append(row)
    return "\n".join(paint(("  " + clipped(line, content_width)).ljust(width),
                           "1;97;48;2;176;0;101", color) for line in lines)


MOUSE_EVENT = re.compile(rb"\x1b\[<(\d+);(\d+);(\d+)([Mm])")


class TerminalInput:
    """Keep terminal escape sequences separate from ordinary shortcut keys."""

    def __init__(self):
        self.state = "key"
        self.sequence = bytearray()

    def feed(self, incoming):
        events = []
        for key in incoming:
            if self.state == "key":
                if key == 27:
                    self.state = "escape"
                    self.sequence = bytearray(b"\x1b")
                else:
                    events.append(("key", key))
            elif self.state == "escape":
                if key in (ord("["), ord("O")):
                    self.sequence.append(key)
                    self.state = "sequence"
                elif key != 27:
                    # Alt-key combinations are not plain shortcut presses.
                    self.state = "key"
            else:
                if len(self.sequence) < 256:
                    self.sequence.append(key)
                if 0x40 <= key <= 0x7e:
                    match = MOUSE_EVENT.fullmatch(self.sequence)
                    if match:
                        button, x, y, action = match.groups()
                        if action == b"M" and int(button) & 99 == 0:
                            events.append(("click", int(x), int(y)))
                    self.state = "key"
                    self.sequence.clear()
                elif key == 27:
                    self.state = "escape"
                    self.sequence = bytearray(b"\x1b")
        return events


def wait_for_refresh(deadline, width, color, input_fd=None, on_click=None,
                     on_save_key=None, on_reset_key=None, on_models_key=None, input_parser=None):
    seconds = max(0, math.ceil(deadline - time.monotonic()))
    input_parser = input_parser if input_parser is not None else TerminalInput()
    while seconds > 0:
        pause = min(1.0, max(0, deadline - time.monotonic()))
        if input_fd is None:
            time.sleep(pause)
        else:
            ready, _, _ = select.select([input_fd], [], [], pause)
            if ready:
                incoming = os.read(input_fd, 128)
                for event in input_parser.feed(incoming):
                    if event[0] == "click":
                        if on_click:
                            on_click(event[1], event[2])
                    elif event[1] in (ord("s"), ord("S")) and on_save_key:
                        on_save_key()
                    elif event[1] in (ord("m"), ord("M")) and on_reset_key:
                        on_reset_key()
                    elif event[1] in (ord("k"), ord("K")) and on_models_key:
                        on_models_key()
        updated = max(0, math.ceil(deadline - time.monotonic()))
        if updated != seconds and updated > 0:
            sys.stdout.write("\033[1A\r\033[2K" + countdown_line(updated, width, color) + "\n")
            sys.stdout.flush()
        seconds = updated


@contextmanager
def interactive_mouse(enabled):
    if not enabled or not sys.stdin.isatty():
        yield None
        return
    try:
        fd = sys.stdin.fileno()
        original = termios.tcgetattr(fd)
    except (OSError, ValueError, termios.error):
        yield None
        return
    try:
        tty.setcbreak(fd)
        sys.stdout.write("\033[?1000h\033[?1006h")
        sys.stdout.flush()
        yield fd
    finally:
        sys.stdout.write("\033[?1000l\033[?1006l")
        sys.stdout.flush()
        termios.tcsetattr(fd, termios.TCSADRAIN, original)


def build_usage(sources):
    """Aggregate (platform_name, per-period rows) sources into totals and a model map."""
    grand = {period: {"cost": 0.0, "cost_cny": 0.0, "tokens": 0} for period, _ in PERIODS}
    platforms = []
    models = {}
    for name, periods in sources:
        totals = {period: {"cost": 0.0, "cost_cny": 0.0, "tokens": 0} for period, _ in PERIODS}
        native_cny = False
        for period, _ in PERIODS:
            for model, row in (periods.get(period) or {}).items():
                tokens = row.get("input", 0) + row.get("output", 0)
                cost = row.get("estimate", 0.0)
                cost_cny = row.get("estimate_cny", 0.0)
                entry = models.setdefault(model, {"platform": name, "unpriced": False,
                                                  **{p: {"cost": 0.0, "tokens": 0}
                                                     for p, _ in PERIODS}})
                entry[period]["cost"] += cost
                entry[period]["tokens"] += tokens
                if row.get("unpriced"):
                    entry["unpriced"] = True
                if "estimate_cny" in row:
                    native_cny = True
                totals[period]["cost"] += cost
                totals[period]["cost_cny"] += cost_cny
                totals[period]["tokens"] += tokens
        platforms.append({"name": name, "currency": "cny" if native_cny else "usd",
                          "totals": totals})
        for period, _ in PERIODS:
            grand[period]["cost"] += totals[period]["cost"]
            grand[period]["cost_cny"] += totals[period]["cost_cny"]
            grand[period]["tokens"] += totals[period]["tokens"]
    files = sum((periods.get("files", 0) or 0) for _, periods in sources)
    unpriced = sum(row.get("unpriced", 0) for _, periods in sources
                   for row in (periods.get("all") or {}).values())
    return {"platforms": platforms, "totals": grand, "models": models,
            "files": files, "unpriced": unpriced}


def sorted_models(usage, period, descending):
    def key(name):
        value = (usage["models"].get(name) or {}).get(period, {}).get("cost")
        return (value is None, -(value or 0) if descending else (value or 0), name)
    return sorted(usage["models"], key=key)


def next_sort(current, descending, clicked):
    return (clicked, not descending if clicked == current else True)


def quota_lines(rows, width, color, indent=2, now_ts=None):
    """Render progress bars for quota rows with a leading indent."""
    pad = " " * indent
    now_ts = time.time() if now_ts is None else now_ts
    labels = [("%s / %s" % (name, window) if name else window).upper()
              for name, window, _, _, _ in rows]
    label_w = max((len(label) for label in labels), default=1)
    lines = []
    for label, (name, window, used, reset, minutes) in zip(labels, rows):
        remaining = round(100 - used)
        percent = paint("%3d%% left" % remaining, "1;" + quota_tone(remaining), color)
        if width < 32:
            bar_width = min(14, max(4, width - indent - 2))
            lines.append("%s%s  %s" % (pad, clipped(label, max(4, width - indent - 12)), percent))
            lines.append("%s%s" % (pad, bar(remaining, bar_width, color)))
            marker = pace_marker(minutes, reset, bar_width, now_ts)
            if marker is not None:
                lines.append(paint(" " * (indent + marker) + PACE_MARKER, PACE_MARKER_COLOR, color))
            lines.append("%sreset %s" % (pad, clipped(when(reset), max(6, width - indent - 7))))
        elif width < 68:
            width_limit = min(max(label_w, 4), max(4, width - indent - 12))
            bar_width = min(24, max(8, width - indent - width_limit - 20))
            lines.append("%s%-*s %s" % (pad, width_limit, clipped(label, width_limit), percent))
            lines.append("%s%s  reset %s" % (pad, bar(remaining, bar_width, color), when(reset)))
            marker = pace_marker(minutes, reset, bar_width, now_ts)
            if marker is not None:
                lines.append(paint(" " * (indent + marker) + PACE_MARKER, PACE_MARKER_COLOR, color))
        else:
            width_limit = max(label_w, 4)
            bar_width = min(32, max(8, width - indent - width_limit - 32))
            bar_start = indent + width_limit + 1
            lines.append("%s%-*s %s  %s  reset %s" %
                         (pad, width_limit, clipped(label, width_limit),
                          bar(remaining, bar_width, color), percent, when(reset)))
            marker = pace_marker(minutes, reset, bar_width, now_ts)
            if marker is not None:
                lines.append(paint(" " * (bar_start + marker) + PACE_MARKER, PACE_MARKER_COLOR, color))
    return lines


def deepseek_balance_lines(balance, hint, width, color, usd_cny=FALLBACK_USD_CNY, indent=4):
    detail = deepseek_money_text(balance, usd_cny) if balance else hint
    if not detail:
        return []
    return keyed_line("BALANCE", detail, width, color, indent=indent)


ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def boxed_lines(title, body, width, color, title_style="1"):
    """Wrap a platform body in a rounded box in the platform's accent color."""
    inner = max(8, width - 6)
    edge = "─" * (width - 4)
def boxed_lines(title, body, width, color, border_style="1", title_style="1;37"):
    """Wrap a platform body in a rounded box; border and title styled separately."""
    inner = max(8, width - 6)
    edge = "─" * (width - 4)
    text = title if len(title) <= inner else title[:inner]
    edge_style = paint("╭" + edge + "╮", border_style, color)
    side = paint("│", border_style, color)
    lines = ["  " + edge_style,
             "  " + side + " " + paint(text, title_style, color)
             + " " * (inner - len(text)) + " " + side]
    for line in body:
        pad = " " * max(0, inner - len(ANSI_RE.sub("", line)))
        lines.append("  " + side + " " + line + pad + " " + side)
    lines.append("  " + paint("╰" + edge + "╯", border_style, color))
    return lines


def cost_pairs(totals, currency="usd"):
    """(label, value) pairs for the four cost periods in USD or CNY."""
    if currency == "cny":
        return [(label.lower(), "¥%.2f" % totals[period].get("cost_cny", 0.0))
                for period, label in PERIODS]
    return [(label.lower(), money(totals[period]["cost"])) for period, label in PERIODS]


def token_pairs(totals):
    """(label, value) pairs for the four token periods."""
    return [(label.lower(), compact_count(totals[period]["tokens"])) for period, label in PERIODS]


def keyed_line(key, value, width, color, indent=4, key_width=9, value_style=None,
               key_style=None, uppercase=True):
    """Render a 'KEY  value' row, wrapping the value beneath it."""
    text = key.upper() if uppercase else key
    hang = " " * (indent + key_width)
    room = max(1, width - len(hang))
    chunks = [value] if len(value) <= room else (textwrap.wrap(
        value, width=room, break_long_words=False, break_on_hyphens=False) or [value])
    lines = []
    for index, chunk in enumerate(chunks):
        if index == 0:
            label = text.ljust(key_width)
            lead = " " * indent + (paint(label, key_style, color) if key_style else label)
        else:
            lead = hang
        lines.append(lead + (paint(chunk, value_style, color) if value_style else chunk))
    return lines


def summary_lines(key, pairs, width, color, indent=4, key_width=9, value_style=None,
                  key_style=None, uppercase=True, accent=None):
    """Render a keyed digest inline, or one period per line when space is tight.

    The value whose label equals `accent` is highlighted green; the rest use
    `value_style` (or the default foreground).
    """
    inline = "  ·  ".join("%s %s" % pair for pair in pairs)
    if len(inline) <= width - indent - key_width:
        text = key.upper() if uppercase else key
        lead = " " * indent + (paint(text.ljust(key_width), key_style, color) if key_style
                               else text.ljust(key_width))
        parts = []
        for label, value in pairs:
            style = "92" if accent and label == accent else value_style
            item = "%s %s" % (label, value)
            parts.append(paint(item, style, color) if style else item)
        return [lead + "  ·  ".join(parts)]
    text = key.upper() if uppercase else key
    lines = [" " * indent + (paint(text, key_style, color) if key_style else text)]
    body = " " * (indent + 4)
    for label, value in pairs:
        style = "92" if accent and label == accent else value_style
        row = clipped(body + "%-6s %s" % (label, value), width)
        lines.append(paint(row, style, color) if style else row)
    return lines


def group_lines(groups):
    """Concatenate non-empty line groups with no extra spacing."""
    lines = []
    for group in groups:
        if group:
            lines.extend(group)
    return lines


def platform_summary_lines(usage, quota, deepseek, deepseek_hint, subscription_info, width, color,
                           usd_cny=FALLBACK_USD_CNY):
    """Per-platform blocks (quota, cost, balance, renewal), empty when unknown."""
    by_name = {platform["name"].lower(): platform for platform in usage["platforms"]}
    now_ts = time.time()
    boxed = width >= 40
    inner = max(20, width - 6) if boxed else width
    indent = 2 if boxed else 4
    blocks = []

    codex = by_name.get("codex")
    cost_group = []
    if codex is not None:
        cost_group.extend(summary_lines("cost", cost_pairs(codex["totals"], codex["currency"]),
                                        inner, color, indent=indent, accent="today"))
        cost_group.append(paint("%s%d session files" % (" " * indent, usage.get("files", 0)),
                                "2", color))
    renewal = subscription_text(subscription_info, max(4, inner - indent - 9))
    codex_body = group_lines([
        quota_lines([(None if str(name).lower() == "codex" else name, window, used, reset, minutes)
                     for name, window, used, reset, minutes in quota],
                    inner, color, indent=indent, now_ts=now_ts),
        cost_group,
        keyed_line("renews", renewal, inner, color, indent=indent,
                   value_style="2", key_style="2", uppercase=False) if renewal else [],
    ])
    if codex_body:
        blocks.append(("CODEX", codex_body))

    deepseek_block = by_name.get("deepseek")
    deepseek_block = by_name.get("deepseek")
    split_lines = (deepseek_split_lines(usage.get("deepseek_split"), inner, color, indent=indent)
                   if deepseek_block is not None else [])
    deep = group_lines([
        deepseek_balance_lines(deepseek, deepseek_hint, inner, color, usd_cny, indent=indent),
        summary_lines("cost", cost_pairs(deepseek_block["totals"], deepseek_block["currency"]),
                      inner, color, indent=indent, accent="today")
        if deepseek_block is not None else [],
        split_lines,
    ])
    if deep:
        blocks.append(("DEEPSEEK", deep))

    for platform in usage["platforms"]:
        if platform["name"].lower() in ("codex", "deepseek"):
            continue
        blocks.append((platform["name"].upper(),
                       summary_lines("cost", cost_pairs(platform["totals"], platform["currency"]),
                                     inner, color, indent=indent, accent="today")))

    lines = []
    for name, body in blocks:
        icon = PLATFORM_ICONS.get(name)
        title = ("%s %s" % (icon, name)) if icon else name
        if boxed:
            lines.extend(boxed_lines(title, body, width, color,
                                     border_style=PLATFORM_COLORS.get(name, "1")))
        else:
            lines.append("  " + paint(title, "1;37", color))
            lines.extend(body)
    return lines


def model_cell(entry, period):
    values = entry.get(period) or {}
    if entry.get("unpriced") and not values.get("cost"):
        return "n/a"
    if not values.get("tokens") and not values.get("cost"):
        return "—"
    return money(values.get("cost", 0.0))


def models_lines(usage, width, color, sort_period, sort_desc, mouse_enabled, mouse_targets,
                 line_offset=0):
    heading = paint("MODELS", "1;36", color)
    if mouse_enabled and width >= 64:
        heading += paint("  Click a column to sort", "2", color)
    elif width >= 28:
        heading += paint("  %d models" % len(usage["models"]), "2", color)
    lines = [heading]
    names = sorted_models(usage, sort_period, sort_desc)
    if width >= 64:
        if mouse_targets is not None:
            mouse_targets["y"] = line_offset + len(lines) + 1
            mouse_targets["columns"] = [(24 + index * 10, 32 + index * 10, period)
                                        for index, (period, _) in enumerate(PERIODS)]
        headers = []
        for period, label in PERIODS:
            title = label + (" ↓" if sort_desc else " ↑") if period == sort_period else label
            headers.append(paint("%9s" % title, "1;33" if period == sort_period else "2", color))
        lines.append(paint("  %-20s " % "MODEL", "2", color) + " ".join(headers))
        for model in names:
            entry = usage["models"][model]
            cells = []
            for period, _ in PERIODS:
                value = model_cell(entry, period)
                cells.append(paint("%9s" % value, "1;92" if value not in ("—", "n/a") else "2", color))
            lines.append("  %-20s %s" % (clipped(model, 20), " ".join(cells)))
    else:
        for model in names:
            entry = usage["models"][model]
            lines.append("  " + clipped(model, max(4, width - 2)))
            for period, label in PERIODS:
                value = model_cell(entry, period)
                lines.append("    %-6s %s" % (label.title(),
                                              paint(value, "1;92" if value not in ("—", "n/a") else "2", color)))
    if not names:
        lines.extend(paint(line, "2", color) for line in textwrap.wrap(
            "No local token records", width=max(18, width - 2),
            initial_indent="  ", subsequent_indent="  "))
    return lines


def render_home(usage, limits, api=None, color=False, width=80, warnings=(), watch=False,
                refresh_in=60, mouse_enabled=False, mouse_targets=None,
                reset_credits=None, save_notice=None, subscription_info=None, pricing_note=None,
                deepseek=None, deepseek_hint=None, usd_cny=FALLBACK_USD_CNY):
    width = max(20, width)
    if mouse_targets is not None:
        mouse_targets.clear()
    title = "noob"
    updated = dt.datetime.now().astimezone().strftime("%b %d  %H:%M")
    if len(title) + len(updated) + 2 <= width:
        title += " " * (width - len(title) - len(updated) - 1) + updated
    quota = quota_rows(limits)
    platform_title = paint("PLATFORMS", "1;37", color)
    if width >= 68 and quota:
        platform_title += paint("  ", "2", color) + paint(PACE_MARKER, PACE_MARKER_COLOR, color)
        platform_title += paint(" even pace", "2", color)
    lines = reset_banner(reset_credits, width, color).splitlines()
    lines += [paint(title, "1;37", color),
              paint("─" * (width - 1), "2", color), platform_title]
    rendered = platform_summary_lines(usage, quota, deepseek, deepseek_hint,
                                      subscription_info, width, color, usd_cny)
    if rendered:
        lines.extend(rendered)
    else:
        lines.extend(textwrap.wrap("No account data available", width=max(18, width - 2),
                                   initial_indent="  ", subsequent_indent="  "))
    lines.append("  " + paint("TOTAL", "1", color))
    lines.extend(group_lines([
        summary_lines("cost", cost_pairs(usage["totals"]), width, color, accent="today"),
        summary_lines("tokens", token_pairs(usage["totals"]), width, color,
                      value_style="2", key_style="2", uppercase=False),
    ]))
    if pricing_note:
        lines.extend(paint(line, "2", color) for line in textwrap.wrap(
            pricing_note, width=max(18, width - 2), initial_indent="  ", subsequent_indent="  "))
    if usage.get("unpriced"):
        lines.extend(paint(line, "2", color) for line in textwrap.wrap(
            "Unpriced tokens excluded: %s." % compact_count(usage["unpriced"]),
            width=max(18, width - 2), initial_indent="  ", subsequent_indent="  "))
    if api is not None:
        lines += [paint("API ORGANIZATION BILLING" if width >= 25 else "API BILLING", "1;36", color)]
        start, costs, buckets = api
        amounts = [item.get("amount") or {} for bucket in costs for item in bucket.get("results", [])]
        usd = sum(float(a.get("value") or 0) for a in amounts if a.get("currency") == "usd")
        other = sorted({a.get("currency") for a in amounts if a.get("currency") not in (None, "usd")})
        lines.extend(textwrap.wrap("Actual month-to-date: $%.2f USD · since %s" %
                    (usd, start.strftime("%Y-%m-%d")), width=max(18, width - 2),
                    initial_indent="  ", subsequent_indent="  "))
        if other:
            lines.extend(textwrap.wrap("Other currencies excluded: %s" % ", ".join(other),
                         width=max(18, width - 2), initial_indent="  ", subsequent_indent="  "))
        totals = model_totals(buckets)
        for model, row in sorted(totals.items(), key=lambda pair: -(pair[1]["input"] + pair[1]["output"])):
            price = RATES.get(model)
            rate_text = ("rates " + " / ".join("$%g" % value if value is not None else "—"
                         for value in price)) if price else "rates unavailable"
            estimated = estimate(model, row)
            lines.extend(textwrap.wrap("%s · %s" % (model, rate_text), width=max(18, width - 2),
                         initial_indent="  ", subsequent_indent="  "))
            lines.extend(textwrap.wrap("{:,} requests · {:,} in ({:,} cached) · {:,} out · reference ${}".format(
                row["requests"], row["input"], row["cached"], row["output"],
                "%.2f" % estimated if estimated is not None else "—"),
                width=max(18, width - 2), initial_indent="    ", subsequent_indent="    "))
        if not totals:
            lines.extend(textwrap.wrap("No model-level completions usage returned this month",
                         width=max(18, width - 2), initial_indent="  ", subsequent_indent="  "))
        lines.extend(textwrap.wrap("Rates: input / cached / cache write / output per 1M tokens; reference is not the invoice",
                     width=max(18, width - 2), initial_indent="  ", subsequent_indent="  "))
    if warnings:
        lines.append(paint("STATUS", "1;33", color))
        for warning in warnings:
            lines.extend(textwrap.wrap(warning, width=max(18, width - 2),
                                       initial_indent="  ", subsequent_indent="  "))
    if save_notice:
        lines.extend(paint(line, "1;92", color) for line in textwrap.wrap(
            save_notice, width=max(18, width - 2), initial_indent="  ",
            subsequent_indent="  "))
    if watch:
        hint = "  K: models · S: card · M: reset card"
        lines.append(paint(hint if len(hint) <= width else "  K · S · M", "2", color))
        lines.append(countdown_line(refresh_in, width, color))
    return "\n".join(lines)


def render_models(usage, color=False, width=80, warnings=(), watch=False, refresh_in=60,
                  sort_period="day", sort_desc=True, mouse_enabled=False, mouse_targets=None,
                  save_notice=None, pricing_note=None):
    width = max(20, width)
    if mouse_targets is not None:
        mouse_targets.clear()
    title = "noob  /  MODELS"
    updated = dt.datetime.now().astimezone().strftime("%b %d  %H:%M")
    if len(title) + len(updated) + 2 <= width:
        title += " " * (width - len(title) - len(updated) - 1) + updated
    lines = [paint(title, "1;37", color), paint("─" * (width - 1), "2", color)]
    lines.extend(models_lines(usage, width, color, sort_period, sort_desc,
                              mouse_enabled, mouse_targets, line_offset=len(lines)))
    if pricing_note:
        lines.extend(paint(line, "2", color) for line in textwrap.wrap(
            pricing_note, width=max(18, width - 2), initial_indent="  ", subsequent_indent="  "))
    if warnings:
        lines.append(paint("STATUS", "1;33", color))
        for warning in warnings:
            lines.extend(textwrap.wrap(warning, width=max(18, width - 2),
                                       initial_indent="  ", subsequent_indent="  "))
    if save_notice:
        lines.extend(paint(line, "1;92", color) for line in textwrap.wrap(
            save_notice, width=max(18, width - 2), initial_indent="  ", subsequent_indent="  "))
    if watch:
        hint = "  K: back · S: models card"
        lines.append(paint(hint if len(hint) <= width else "  K · S", "2", color))
        lines.append(countdown_line(refresh_in, width, color))
    return "\n".join(lines)


def render_loading(width=80, color=False):
    return "\n".join((paint("noob", "1;37", color),
                      paint("─" * (max(20, width) - 1), "2", color),
                      "  " + paint("Loading...", "1;36", color)))


def main(argv=None):
    parser = argparse.ArgumentParser(prog="noob", description="Show subscription quotas, API balances, and the API-price equivalent of local token usage")
    parser.add_argument("--no-color", action="store_true", help="disable ANSI colors")
    parser.add_argument("--once", action="store_true", help="show one snapshot without auto-refresh")
    parser.add_argument("--api", action="store_true", help="also query API organization billing (requires OPENAI_ADMIN_KEY)")
    parser.add_argument("--api-only", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    api_requested = args.api or args.api_only
    interactive = sys.stdout.isatty()
    color = interactive and not args.no_color
    width = shutil.get_terminal_size((80, 24)).columns
    watch = interactive and not args.once and not api_requested
    sort_period, sort_desc = "day", True
    view = "home"
    reset_credits = None
    subscription_info = load_subscription_info()
    save_notice = None
    first_query = True
    if interactive:
        sys.stdout.write("\033[H\033[J" + render_loading(width, color) + "\n")
        sys.stdout.flush()
    try:
        with interactive_mouse(watch) as input_fd:
            input_parser = TerminalInput()
            prices, pricing_note = refresh_prices()
            if prices:
                RATES.update(prices["rates"])
                for model in prices["rates"]:
                    LONG_RATES.pop(model, None)
                LONG_RATES.update(prices["long_rates"])
                LONG_CONTEXT_MODELS.difference_update(prices["rates"])
                LONG_CONTEXT_MODELS.update(prices["long_rates"])
            deepseek_key = deepseek_api_key()
            usd_cny = fetch_usd_cny() or FALLBACK_USD_CNY
            deepseek_rates = deepseek_prices()
            deepseek_versions = load_deepseek_models()
            if record_deepseek_versions(deepseek_versions, fetch_deepseek_models(deepseek_key)):
                save_deepseek_models(deepseek_versions)
            while True:
                started = time.monotonic()
                warnings = []
                limits = {}
                try:
                    limits = codex_data()
                    if first_query:
                        reset_credits = limits.get("rateLimitResetCredits")
                except (OSError, RuntimeError, ValueError) as exc:
                    warnings.append("Codex quota query failed: %s" % exc)
                first_query = False
                sources = []
                try:
                    sources.append(("Codex", scan_local_sessions()))
                except OSError as exc:
                    warnings.append("Could not read local session logs: %s" % exc)
                opencode_rows = None
                pi_rows = None
                deepseek_sources = []
                if shutil.which("opencode"):
                    try:
                        opencode_rows = opencode_usage(
                            rates=deepseek_rates, usd_cny=usd_cny, versions=deepseek_versions)
                        deepseek_sources.append(opencode_rows)
                    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as exc:
                        warnings.append("OpenCode usage query failed: %s" % exc)
                try:
                    pi_rows = pi_usage(usd_cny=usd_cny, versions=deepseek_versions,
                                       rates=deepseek_rates)
                    deepseek_sources.append(pi_rows)
                except (OSError, RuntimeError, ValueError) as exc:
                    warnings.append("pi usage query failed: %s" % exc)
                if deepseek_sources:
                    sources.append(("DeepSeek", merge_deepseek_usage(*deepseek_sources)))
                usage = build_usage(sources)
                usage["deepseek_split"] = deepseek_split_costs(opencode_rows, pi_rows)
                api = None
                if api_requested:
                    key = os.environ.get("OPENAI_ADMIN_KEY")
                    if not key:
                        warnings.append("Set OPENAI_ADMIN_KEY to query API organization billing")
                    else:
                        try:
                            api = api_data(key)
                        except (RuntimeError, ValueError) as exc:
                            warnings.append("API billing query failed: %s" % exc)
                deepseek = None
                deepseek_hint = None
                if deepseek_key:
                    try:
                        deepseek = deepseek_balance(deepseek_key)
                    except (RuntimeError, ValueError) as exc:
                        deepseek_hint = "unavailable"
                        warnings.append("DeepSeek balance query failed: %s" % exc)
                else:
                    deepseek_hint = "set DEEPSEEK_API_KEY"
                deadline = started + 60
                mouse_targets = {}
                current_output = ""
                current_width = width

                def dashboard(display_width):
                    refresh_in = max(0, math.ceil(deadline - time.monotonic()))
                    if view == "detail":
                        return render_models(usage, color=color, width=display_width,
                                             warnings=warnings, watch=watch, refresh_in=refresh_in,
                                             sort_period=sort_period, sort_desc=sort_desc,
                                             mouse_enabled=input_fd is not None,
                                             mouse_targets=mouse_targets, save_notice=save_notice,
                                             pricing_note=pricing_note)
                    return render_home(usage, limits, api, color=color, width=display_width,
                                       warnings=warnings, watch=watch, refresh_in=refresh_in,
                                       mouse_enabled=input_fd is not None, mouse_targets=mouse_targets,
                                       reset_credits=reset_credits, save_notice=save_notice,
                                       subscription_info=subscription_info, pricing_note=pricing_note,
                                       deepseek=deepseek, deepseek_hint=deepseek_hint, usd_cny=usd_cny)

                def redraw():
                    nonlocal current_output, current_width
                    current_width = shutil.get_terminal_size((80, 24)).columns
                    current_output = dashboard(current_width)
                    if interactive:
                        sys.stdout.write("\033[H\033[J" + current_output + "\n")
                        sys.stdout.flush()
                    else:
                        print(current_output)

                def toggle_models():
                    nonlocal view
                    view = "detail" if view == "home" else "home"
                    redraw()

                def sort_on_click(x, y):
                    nonlocal sort_period, sort_desc
                    if view != "detail" or y != mouse_targets.get("y"):
                        return
                    for left, right, period in mouse_targets.get("columns", []):
                        if left <= x <= right:
                            sort_period, sort_desc = next_sort(sort_period, sort_desc, period)
                            redraw()
                            return

                def fresh_sources():
                    collected = []
                    opencode_rows = None
                    pi_rows = None
                    deepseek_sources = []
                    try:
                        collected.append(("Codex", scan_local_sessions()))
                    except OSError:
                        pass
                    if shutil.which("opencode"):
                        try:
                            opencode_rows = opencode_usage(
                                rates=deepseek_rates, usd_cny=usd_cny, versions=deepseek_versions)
                            deepseek_sources.append(opencode_rows)
                        except (OSError, RuntimeError, ValueError, subprocess.SubprocessError):
                            pass
                    try:
                        pi_rows = pi_usage(usd_cny=usd_cny, versions=deepseek_versions,
                                           rates=deepseek_rates)
                        deepseek_sources.append(pi_rows)
                    except (OSError, RuntimeError, ValueError):
                        pass
                    if deepseek_sources:
                        collected.append(("DeepSeek", merge_deepseek_usage(*deepseek_sources)))
                    return collected, deepseek_split_costs(opencode_rows, pi_rows)

                def save_image():
                    nonlocal usage, limits, save_notice
                    try:
                        image_limits = codex_data()
                        limits = image_limits
                    except (OSError, RuntimeError, ValueError) as exc:
                        image_limits = {}
                        warnings.append("Could not refresh Codex quota for image: %s" % exc)
                    try:
                        collected, split = fresh_sources()
                        usage = build_usage(collected)
                        usage["deepseek_split"] = split
                        path = save_card(usage, quotas=card_quotas(image_limits),
                                         deepseek=deepseek_money_text(deepseek, usd_cny))
                        save_notice = "Saved image: %s" % path
                    except (OSError, RuntimeError, ValueError) as exc:
                        save_notice = None
                        warnings.append("Could not save image: %s" % exc)
                    redraw()

                def save_models_image():
                    nonlocal save_notice
                    try:
                        path = save_models_card(usage, sort_period=sort_period, sort_desc=sort_desc)
                        save_notice = "Saved models card: %s" % path
                    except (OSError, RuntimeError, ValueError) as exc:
                        save_notice = None
                        warnings.append("Could not save models card: %s" % exc)
                    redraw()

                def save_current():
                    if view == "detail":
                        save_models_image()
                    else:
                        save_image()

                def save_reset_image():
                    nonlocal limits, save_notice
                    try:
                        image_limits = codex_data()
                        limits = image_limits
                        path = save_reset_card(image_limits.get("rateLimitResetCredits"))
                        save_notice = "Saved reset card: %s" % path
                    except (OSError, RuntimeError, ValueError) as exc:
                        save_notice = None
                        warnings.append("Could not save reset card: %s" % exc)
                    redraw()

                redraw()
                if not watch:
                    return 1 if warnings else 0
                wait_for_refresh(deadline, width, color, input_fd=input_fd,
                                 on_click=sort_on_click,
                                 on_save_key=save_current, on_reset_key=save_reset_image,
                                 on_models_key=toggle_models,
                                 input_parser=input_parser)
    except KeyboardInterrupt:
        if interactive:
            print()
        return 0


if __name__ == "__main__":
    sys.exit(main())
