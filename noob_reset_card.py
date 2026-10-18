"""Render the noobsolo reset-credit card from a fresh Codex account snapshot."""

import datetime as dt
import math
import secrets
import xml.etree.ElementTree as ET
from pathlib import Path

from noob_card import save_svg_png


SVG_NS = "http://www.w3.org/2000/svg"
ET.register_namespace("", SVG_NS)
TEMPLATE = Path(__file__).with_name("noob-reset-template.svg")


def _node(root, node_id):
    found = root.find(".//*[@id='%s']" % node_id)
    if found is None:
        raise ValueError("Reset card template is missing %s" % node_id)
    return found


def _add(parent, tag, **attributes):
    return ET.SubElement(parent, "{%s}%s" % (SVG_NS, tag),
                         {key.replace("_", "-"): str(value) for key, value in attributes.items()})


def _text(parent, x, y, value, color, size, weight, spacing=0, anchor=None):
    attributes = {"x": x, "y": round(y, 2), "fill": color, "font_size": size,
                  "font_weight": weight, "letter_spacing": spacing}
    if anchor:
        attributes["text_anchor"] = anchor
    element = _add(parent, "text", **attributes)
    element.text = value
    return element


def _expiry(item):
    value = item.get("expiresAt")
    if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value):
        return None
    try:
        return dt.datetime.fromtimestamp(value).astimezone()
    except (OSError, OverflowError, ValueError):
        return None


def reset_card_svg(credits, now=None):
    """Build the selected pink timeline layout with actual credit expiries."""
    if not isinstance(credits, dict):
        raise ValueError("Reset-credit data is unavailable")
    count = credits.get("availableCount")
    if not isinstance(count, int) or isinstance(count, bool) or count < 0:
        raise ValueError("Reset-credit count is unavailable")
    items = credits.get("credits", [])
    if not isinstance(items, list):
        raise ValueError("Reset-credit list is unavailable")
    available = [item for item in items if isinstance(item, dict)
                 and item.get("status") == "available"]
    if len(available) > count:
        raise ValueError("Reset-credit count does not match the available list")
    expiries = [_expiry(item) for item in available]
    expiries.sort(key=lambda expiry: (expiry is None,
                                      expiry.timestamp() if expiry else float("inf")))
    expiries += [None] * (count - len(expiries))

    now = now or dt.datetime.now().astimezone()
    if now.tzinfo is None:
        now = now.astimezone()
    root = ET.parse(TEMPLATE).getroot()
    _node(root, "card-date").text = "RESET CARD / " + now.strftime("%d %b %Y").upper()
    count_node = _node(root, "available-count")
    count_node.text = "%02d" % count
    if count >= 100:
        count_node.set("font-size", "145")
        count_node.set("letter-spacing", "-12")
    _node(root, "card-status").text = "ACCOUNT SNAPSHOT · " + now.strftime("%H:%M")
    offset = now.strftime("%z")
    timezone = ("UTC%s%d" % (offset[0], int(offset[1:3])) +
                (":" + offset[3:] if offset[3:] != "00" else "")) if offset else "LOCAL TIME"
    _node(root, "timeline-timezone").text = "LOCAL · " + timezone
    _node(root, "card-timezone").text = "EXPIRY TIMES · LOCAL " + timezone

    rows = _node(root, "expiry-rows")
    if count == 0:
        _text(rows, 191, 880, "NO RESET CREDITS AVAILABLE", "#f1f5f7", 34, 700)
        _node(root, "card-note").text = "NO CREDITS AVAILABLE AT THIS SNAPSHOT"
    else:
        visible = expiries[:20]
        if count > len(visible):
            _node(root, "card-note").text = "SHOWING FIRST 20 OF %d EXPIRIES" % count
        elif any(expiry is None for expiry in visible):
            _node(root, "card-note").text = "SOME EXPIRY TIMES WERE NOT REPORTED"
        if count <= 10 and len(visible) > 1:
            first_y, last_y = ((661, 1057) if count >= 3 else (760, 958))
            _add(rows, "line", x1=137, y1=first_y - 14, x2=137, y2=last_y,
                 stroke="#39454d", stroke_width=3)
        elif count > 10:
            for column in range((len(visible) + 9) // 10):
                rail_x = 137 + column * 466
                _add(rows, "line", x1=rail_x, y1=633, x2=rail_x, y2=1057,
                     stroke="#39454d", stroke_width=3)
        for index, expiry in enumerate(visible):
            if count <= 3:
                y = {1: 859, 2: 760, 3: 661}[count] + index * (198 if count > 1 else 0)
                rail_x, date_x, time_x = 137, 191, 992
                date_size, time_size, note_size, note_gap = 37, 50, 17, 57
            elif count <= 6:
                y = 661 + index * 396 / (count - 1)
                rail_x, date_x, time_x = 137, 191, 992
                date_size, time_size, note_size, note_gap = 32, 43, 15, 50
            elif count <= 10:
                y = 647 + index * 410 / (count - 1)
                rail_x, date_x, time_x = 137, 191, 992
                date_size, time_size, note_size, note_gap = 25, 33, 12, 27
            else:
                column, position = divmod(index, 10)
                y = 647 + position * 410 / 9
                rail_x = 137 + column * 466
                date_x = 191 + column * 466
                time_x = 520 + column * 472
                date_size, time_size, note_size, note_gap = 20, 26, 11, 24
            _add(rows, "rect", x=rail_x - 11, y=round(y - 11, 2), width=22,
                 height=22, rx=4, fill="#e96091")
            if expiry is None:
                date_text, time_text = "DATE UNKNOWN", "—"
            else:
                date_text = expiry.strftime("%d %b").upper()
                if expiry.year != now.year:
                    date_text += " %d" % expiry.year
                time_text = expiry.strftime("%H:%M")
            _text(rows, date_x, y + 12, date_text, "#f1f5f7", date_size, 700, 1)
            _text(rows, time_x, y + 12, time_text, "#f1f5f7", time_size, 800, -2, "end")
            _text(rows, date_x, y + note_gap, "RESET CREDIT  %02d" % (index + 1),
                  "#79838e", note_size, 600, 2)
    return ET.tostring(root, encoding="unicode")


def save_reset_card(credits, directory=None, now=None):
    """Save a 1080 × 1350 reset-credit PNG and return its path."""
    now = now or dt.datetime.now().astimezone()
    image = reset_card_svg(credits, now)
    directory = Path(directory) if directory is not None else Path.home() / "Downloads"
    directory.mkdir(parents=True, exist_ok=True)
    name = "noobsolo-reset-%s-%s.png" % (now.strftime("%Y%m%d-%H%M%S"), secrets.token_hex(2))
    destination = directory / name
    save_svg_png(image, destination)
    return destination
