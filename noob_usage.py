"""Append-only local usage ledger.

Every capture appends priced token increments, each carrying the rate snapshot
that was in effect when it was recorded. Historical costs therefore never move
when a price table changes, and they survive the deletion of the original
Codex/OpenCode/pi logs.

The ledger is local-only and gitignored. It lives next to the program so every
checkout keeps its own private history, while the parsing and pricing logic
itself stays in the repository.
"""

import datetime as dt
import fcntl
import json
import os
from contextlib import contextmanager
from pathlib import Path


SCHEMA = 1
PERIODS = ("day", "week", "month", "all")
TOKEN_FIELDS = ("input", "cached", "write", "output")


def default_directory():
    override = os.environ.get("NOOB_USAGE_DIR")
    if override:
        return Path(override).expanduser()
    # Lives next to the program and is gitignored: every checkout keeps its own
    # private ledger, and the updater never sees a dirty tracked file.
    return Path(__file__).resolve().parent / "usage"


def empty_state():
    return {
        "version": SCHEMA,
        "codex": {"sessions": {}},
        "pi": {"files": {}},
        "opencode": {"days": {}},
        "baseline_only": False,
    }


def empty_row():
    return {"input": 0, "cached": 0, "write": 0, "output": 0,
            "estimate": 0.0, "unpriced": 0, "calls": 0}


def period_starts(now):
    """Local date boundaries used by the dashboard."""
    day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return {
        "day": day.date(),
        "week": (day - dt.timedelta(days=day.weekday())).date(),
        "month": day.replace(day=1).date(),
        "all": None,
    }


def _clean_state(raw):
    state = empty_state()
    if not isinstance(raw, dict):
        return state
    for section in ("codex", "pi", "opencode"):
        value = raw.get(section)
        if isinstance(value, dict):
            state[section].update(value)
    for section, key in (("codex", "sessions"), ("pi", "files"), ("opencode", "days")):
        if not isinstance(state[section].get(key), dict):
            state[section][key] = {}
    state["baseline_only"] = bool(raw.get("baseline_only"))
    return state


class UsageLedger:
    """Local append-only ledger plus capture watermarks."""

    def __init__(self, directory=None):
        self.directory = Path(directory).expanduser() if directory else default_directory()
        self.events_dir = self.directory / "events"
        self.state_path = self.directory / "state.json"
        self.lock_path = self.directory / ".lock"

    # -- storage ---------------------------------------------------------

    @contextmanager
    def _lock(self):
        self.directory.mkdir(parents=True, exist_ok=True)
        with self.lock_path.open("a+") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    def _event_files(self):
        if not self.events_dir.is_dir():
            return []
        return sorted(self.events_dir.glob("*.jsonl"))

    def has_events(self):
        for path in self._event_files():
            try:
                if path.stat().st_size:
                    return True
            except OSError:
                continue
        return False

    def read_events(self):
        events = []
        for path in self._event_files():
            try:
                with path.open("r", encoding="utf-8") as stream:
                    for line in stream:
                        try:
                            event = json.loads(line)
                        except ValueError:
                            continue
                        if valid_event(event):
                            events.append(event)
            except OSError:
                continue
        return events

    def _load_state(self):
        try:
            return _clean_state(json.loads(self.state_path.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            return empty_state()

    def _write_state(self, state):
        self.directory.mkdir(parents=True, exist_ok=True)
        temporary = self.state_path.with_name("." + self.state_path.name + ".tmp")
        try:
            temporary.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n",
                                  encoding="utf-8")
            os.replace(temporary, self.state_path)
        finally:
            temporary.unlink(missing_ok=True)

    def _append_events(self, events):
        if not events:
            return
        self.events_dir.mkdir(parents=True, exist_ok=True)
        by_month = {}
        for event in events:
            by_month.setdefault(event["day"][:7], []).append(event)
        for month, rows in by_month.items():
            path = self.events_dir / ("%s.jsonl" % month)
            with path.open("a", encoding="utf-8") as stream:
                for event in rows:
                    stream.write(json.dumps(event, sort_keys=True) + "\n")
                stream.flush()
                os.fsync(stream.fileno())

    @contextmanager
    def transaction(self):
        """Hold the capture lock while state is read, extended, and written."""
        with self._lock():
            state = self._load_state()
            events = []
            yield state, events
            self._append_events(events)
            self._write_state(state)

    # -- reporting -------------------------------------------------------

    def aggregate(self, now=None, source=None):
        """{platform: {period: {display: row}}} from all stored events."""
        now = now or dt.datetime.now().astimezone()
        starts = period_starts(now)
        today = now.date()
        rows = {}
        for event in self.read_events():
            if source is not None and event.get("source") != source:
                continue
            try:
                event_day = dt.date.fromisoformat(event["day"])
            except (KeyError, TypeError, ValueError):
                continue
            if event_day > today:
                continue
            platform = event.get("platform")
            if platform not in ("Codex", "DeepSeek"):
                continue
            display = event.get("display") or event.get("model") or "Unknown model"
            tokens = event.get("tokens") or {}
            for period in PERIODS:
                start = starts[period]
                if start is not None and event_day < start:
                    continue
                bucket = rows.setdefault(platform, {}).setdefault(period, {})
                row = bucket.setdefault(display, empty_row())
                row["input"] += int(tokens.get("input") or 0)
                row["cached"] += int(tokens.get("cached") or 0)
                row["write"] += int(tokens.get("write") or 0)
                row["output"] += int(tokens.get("output") or 0)
                row["calls"] += int(event.get("calls") or 0)
                row["unpriced"] += int(event.get("unpriced") or 0)
                if event.get("currency") == "CNY":
                    row.setdefault("estimate_cny", 0.0)
                    row["estimate"] += float(event.get("cost_usd") or 0.0)
                    row["estimate_cny"] += float(event.get("cost_cny") or 0.0)
                else:
                    row["estimate"] += float(event.get("cost") or 0.0)
        return rows

    def period_rows(self, platform, now=None, source=None):
        return (self.aggregate(now, source=source).get(platform) or
                {period: {} for period in PERIODS})


def valid_event(event):
    if not isinstance(event, dict):
        return False
    if not isinstance(event.get("day"), str) or len(event["day"]) != 10:
        return False
    if not isinstance(event.get("tokens"), dict):
        return False
    return any(type(event["tokens"].get(field)) in (int, float)
               for field in TOKEN_FIELDS)
