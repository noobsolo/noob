"""Render the noobsolo local-usage share card as a PNG on macOS."""

import datetime as dt
import os
import random
import secrets
import struct
import subprocess
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path


SVG_NS = "http://www.w3.org/2000/svg"
ET.register_namespace("", SVG_NS)
TEMPLATE = Path(__file__).with_name("noob-card-demo.svg")
MODELS_TEMPLATE = Path(__file__).with_name("noob-models-template.svg")
PERIODS = ("day", "week", "month", "all")
PALETTES = (
    ("#78eed6", "#ee9bab", "#2a6870"),
    ("#76ceff", "#ff91c7", "#315d8d"),
    ("#b7a2ff", "#75eed4", "#564a88"),
    ("#ffbd7d", "#88f4db", "#826148"),
)
QUOTA_COLORS = {"32": "#62d98b", "33": "#efc567", "31": "#f27b70"}


def _node(root, node_id):
    found = root.find(".//*[@id='%s']" % node_id)
    if found is None:
        raise ValueError("Card template is missing %s" % node_id)
    return found


def _money(value):
    return "$%.2f" % value


def _platform_money(platform, period):
    """Period cost in the platform's own currency (DeepSeek reports CNY)."""
    totals = platform["totals"][period]
    if platform.get("currency") == "cny":
        return "¥%.2f" % totals.get("cost_cny", 0.0)
    return _money(totals["cost"])


def _compact(value):
    for size, suffix in ((1_000_000_000, "B"), (1_000_000, "M"), (1_000, "K")):
        if value >= size:
            return "%.1f%s" % (value / size, suffix)
    return str(value)


def _set_money(root, node_id, amount, base_size, cents_size):
    node = _node(root, node_id)
    whole, cents = _money(amount).split(".")
    node.text = whole
    node[0].text = "." + cents
    # Preserve the poster's large type while keeping unusual large totals inside the card.
    scale = min(1.0, 7.0 / max(7, len(whole) + 2))
    node.set("font-size", str(round(base_size * scale)))
    node[0].set("font-size", str(round(cents_size * scale)))


def _set_quota_bar(root, period, quota):
    fill = _node(root, "quota-%s-fill" % period)
    unavailable = _node(root, "quota-%s-unavailable" % period)
    if quota is None:
        fill.set("width", "0")
        unavailable.text = "—"
    else:
        remaining, tone = quota
        fill.set("width", str(round(161 * max(0.0, min(100.0, remaining)) / 100, 2)))
        fill.set("fill", QUOTA_COLORS[tone])
        unavailable.text = ""


def _randomize_avatar(root, rng):
    avatar = _node(root, "avatar-art")
    mint, pink, shadow = rng.choice(PALETTES)
    colors = {"#78eed6": mint, "#7af4df": mint, "#82f9e2": mint,
              "#73efd8": mint, "#ee9bab": pink, "#f09bac": pink,
              "#d588a4": pink, "#2a6870": shadow}
    for child in avatar.iter():
        for attribute in ("fill", "stroke"):
            current = child.get(attribute)
            if current in colors:
                child.set(attribute, colors[current])

    # Eyes, jaw and edge signals vary independently, so the avatar is generated
    # afresh even when two captures happen to pick the same color palette.
    eyes = next((child for child in avatar if child.get("fill") == "#0a2730"), None)
    eye_width = rng.choice((13, 16, 19, 22))
    eye_height = rng.choice((7, 9, 11))
    if eyes is not None:
        eyes.set("d", "M129 203h{0}v{1}h-{0}z M{2} 203h{0}v{1}h-{0}z".format(
            eye_width, eye_height, 175 - eye_width))
    jaw = next((child for child in avatar if child.get("d", "").startswith("M128 231")), None)
    if jaw is not None and rng.randrange(2):
        jaw.set("d", "M128 231h48v7h-13v8h-22v-8h-13z")
    for _ in range(rng.randint(3, 6)):
        x = rng.choice((94, 98, 195, 199, 203))
        y = rng.randrange(184, 246, 7)
        ET.SubElement(avatar, "{%s}rect" % SVG_NS,
                      {"x": str(x), "y": str(y), "width": str(rng.choice((4, 6, 9))),
                       "height": str(rng.choice((2, 3, 5))),
                       "fill": rng.choice((mint, pink)), "opacity": ".85"})


def _value_text(entry, period):
    values = entry.get(period) or {}
    if entry.get("unpriced") and not values.get("cost"):
        return "n/a"
    if not values.get("tokens") and not values.get("cost"):
        return "—"
    return _money(values.get("cost", 0.0))


def card_svg(usage, now=None, rng=None, quotas=None, deepseek=None):
    """Build the home card from aggregated usage and the latest quota snapshot."""
    now = now or dt.datetime.now().astimezone()
    rng = rng or random.Random(secrets.randbits(64))
    root = ET.parse(TEMPLATE).getroot()
    _randomize_avatar(root, rng)

    totals = usage["totals"]
    _node(root, "card-date").text = now.strftime("%d %b %Y").upper()
    _node(root, "tokens").text = _compact(totals["all"]["tokens"]) + " "
    _node(root, "sessions").text = str(usage.get("files", 0)) + " "
    _node(root, "models").text = str(len(usage["models"])) + " "

    _set_money(root, "today-amount", totals["day"]["cost"], 86, 57)
    for node_id in ("month-amount-glow", "month-amount"):
        _set_money(root, node_id, totals["month"]["cost"], 109, 65)
    _node(root, "week-amount").text = _money(totals["week"]["cost"])
    _node(root, "all-amount").text = _money(totals["all"]["cost"])

    ordered = sorted(usage["platforms"],
                     key=lambda item: (-item["totals"]["month"]["cost"], item["name"]))
    main = ordered[:3]
    if len(ordered) > 3:
        rest = ordered[2:]
        # Extra platforms fold into one USD row so none is dropped from the card.
        other = {"name": "OTHER", "currency": "usd", "totals": {
            period: {"cost": sum(item["totals"][period]["cost"] for item in rest),
                     "cost_cny": 0.0} for period in PERIODS}}
        main = ordered[:2] + [other]

    split = usage.get("deepseek_split") or {}
    rows = []  # (label, is_sub, [today, week, month])
    for platform in main:
        rows.append((platform["name"].upper()[:14], False,
                     [_platform_money(platform, period) for period in PERIODS[:3]]))
        if platform["name"].lower() == "deepseek":
            for source in ("opencode", "pi"):
                cny = ((split.get(source) or {}).get("cny")) or {}
                rows.append((source, True,
                             ["¥%.2f" % (cny.get(period) or 0.0) for period in PERIODS[:3]]))

    rows_node = _node(root, "platform-rows")
    for index, (label, is_sub, values) in enumerate(rows):
        y = 988 + index * 44
        fill = "#5b6670" if is_sub else "#79838e"
        size = 19 if is_sub else 22
        weight = 600 if is_sub else 700
        name_el = ET.SubElement(rows_node, "{%s}text" % SVG_NS, {
            "x": str(106 if is_sub else 88), "y": str(y), "class": "mono",
            "fill": fill, "font-size": str(size), "font-weight": str(weight),
            "letter-spacing": "1"})
        name_el.text = label
        for x, value in zip((630, 810, 992), values):
            cell = ET.SubElement(rows_node, "{%s}text" % SVG_NS, {
                "x": str(x), "y": str(y), "text-anchor": "end", "class": "mono",
                "fill": fill, "font-size": str(size), "font-weight": str(weight)})
            cell.text = value
    if not rows:
        empty = ET.SubElement(rows_node, "{%s}text" % SVG_NS, {
            "x": "88", "y": "988", "class": "mono dim", "font-size": "22"})
        empty.text = "NO LOCAL USAGE"

    # The footer hugs the cost table: shift it one row height per extra/missing row.
    _node(root, "card-footer").set(
        "transform", "translate(0,%d)" % ((max(1, len(rows)) - 3) * 44))

    _node(root, "deepseek-balance").text = deepseek or "—"
    quotas = quotas or {}
    _set_quota_bar(root, "5h", quotas.get(300))
    _set_quota_bar(root, "week", quotas.get(10080))
    return ET.tostring(root, encoding="unicode")


def models_card_svg(usage, now=None, sort_period="month", sort_desc=True):
    """Build the detail card listing every model and its per-period cost."""
    now = now or dt.datetime.now().astimezone()
    root = ET.parse(MODELS_TEMPLATE).getroot()
    _node(root, "card-date").text = now.strftime("%d %b %Y").upper()
    for period, label in (("day", "TODAY"), ("week", "WEEK"), ("month", "MONTH"), ("all", "ALL")):
        head = _node(root, "head-%s" % period)
        head.set("class", "mono mint" if period == sort_period else "mono dim")
        head.text = (label + (" ↓" if sort_desc else " ↑")) if period == sort_period else label
    rows_node = _node(root, "model-rows")

    def sort_key(item):
        values = item[1].get(sort_period) or {}
        has_usage = bool(values.get("tokens") or values.get("cost"))
        value = values.get("cost", 0.0) or 0.0
        return (not has_usage, -value if sort_desc else value, item[0])

    entries = sorted(usage["models"].items(), key=sort_key)
    max_rows = 13
    shown = entries[:max_rows]
    if len(entries) > max_rows:
        rest = entries[max_rows - 1:]
        other = {"unpriced": any(entry.get("unpriced") for _, entry in rest)}
        for period in PERIODS:
            other[period] = {"cost": sum(entry[period]["cost"] for _, entry in rest),
                             "tokens": sum(entry[period]["tokens"] for _, entry in rest)}
        shown = entries[:max_rows - 1] + [("OTHER", other)]
    for index, (name, entry) in enumerate(shown):
        y = 452 + index * 58
        label = ET.SubElement(rows_node, "{%s}text" % SVG_NS, {
            "x": "88", "y": str(y), "class": "mono white",
            "font-size": "26", "font-weight": "800"})
        label.text = name.upper()[:26]
        for x, period in ((520, "day"), (660, "week"), (800, "month"), (992, "all")):
            cell = ET.SubElement(rows_node, "{%s}text" % SVG_NS, {
                "x": str(x), "y": str(y), "text-anchor": "end",
                "class": "mono white", "font-size": "26", "font-weight": "800"})
            cell.text = _value_text(entry, period)
    if not shown:
        empty = ET.SubElement(rows_node, "{%s}text" % SVG_NS, {
            "x": "88", "y": "452", "class": "mono dim",
            "font-size": "26", "font-weight": "600"})
        empty.text = "NO USAGE RECORDED"
    _node(root, "card-status").text = "%d MODELS" % len(usage["models"])
    return ET.tostring(root, encoding="unicode")


def save_card(usage, directory=None, now=None, rng=None, quotas=None, deepseek=None):
    """Save a 1080 × 1350 PNG to Downloads and return its path."""
    now = now or dt.datetime.now().astimezone()
    directory = Path(directory) if directory is not None else Path.home() / "Downloads"
    directory.mkdir(parents=True, exist_ok=True)
    name = "noobsolo-usage-%s-%s.png" % (now.strftime("%Y%m%d-%H%M%S"), secrets.token_hex(2))
    destination = directory / name
    image = card_svg(usage, now, rng, quotas, deepseek)
    save_svg_png(image, destination)
    return destination


def save_models_card(usage, directory=None, now=None, sort_period="month", sort_desc=True):
    """Save the detail model-cost card and return its path."""
    now = now or dt.datetime.now().astimezone()
    directory = Path(directory) if directory is not None else Path.home() / "Downloads"
    directory.mkdir(parents=True, exist_ok=True)
    name = "noobsolo-models-%s-%s.png" % (now.strftime("%Y%m%d-%H%M%S"), secrets.token_hex(2))
    destination = directory / name
    save_svg_png(models_card_svg(usage, now, sort_period, sort_desc), destination)
    return destination


def save_svg_png(image, destination):
    """Render a 1080 × 1350 SVG card to PNG and atomically replace destination."""
    destination = Path(destination)
    partial = destination.parent / ("." + destination.name + ".tmp.png")
    wrapper = ('<svg xmlns="http://www.w3.org/2000/svg" width="1350" height="1350" '
               'viewBox="0 0 1350 1350"><svg x="135" y="0" width="1080" '
               'height="1350" viewBox="0 0 1080 1350">%s</svg></svg>' % image)
    try:
        with tempfile.TemporaryDirectory(prefix="noob-card-") as folder:
            source = Path(folder) / "card.svg"
            source.write_text(wrapper, encoding="utf-8")
            try:
                subprocess.run(["qlmanage", "-t", "-s", "1350", "-o", folder, str(source)],
                               check=True, capture_output=True, text=True, timeout=30)
                thumbnail = Path(folder) / "card.svg.png"
                if not thumbnail.is_file():
                    raise RuntimeError("Quick Look did not create a thumbnail")
                subprocess.run(["sips", "--cropToHeightWidth", "1350", "1080",
                                "--cropOffset", "0", "135", str(thumbnail),
                                "--out", str(partial)],
                               check=True, capture_output=True, text=True, timeout=30)
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
                raise RuntimeError("Could not render the usage card") from exc
        with partial.open("rb") as stream:
            header = stream.read(24)
        if header[:8] != b"\x89PNG\r\n\x1a\n" or struct.unpack(">II", header[16:24]) != (1080, 1350):
            raise RuntimeError("The rendered card has an unexpected image size")
        os.replace(partial, destination)
    finally:
        partial.unlink(missing_ok=True)
