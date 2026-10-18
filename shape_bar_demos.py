#!/usr/bin/env python3
"""Preview the fixed solid/outline quota bar."""

import argparse
import sys
import unicodedata


# Original design 15: solid means remaining; outline means used.
SHAPE = ("▰", "▱")


def paint(text, style, color):
    return "\033[%sm%s\033[0m" % (style, text) if color and text else text


def shape_cell_width(shape):
    return 2 if any(unicodedata.east_asian_width(glyph) in ("W", "F")
                    for glyph in shape) else 1


def render_shape(shape, remaining, width=20, color=False):
    cell_width = shape_cell_width(shape)
    slots = width // cell_width
    filled = max(0, min(slots, round(slots * remaining / 100)))
    tone = "32" if remaining >= 60 else "33" if remaining >= 20 else "31"
    solid, outline = shape
    return (paint(solid * filled, tone, color) +
            paint(outline * (slots - filled), "2", color) +
            " " * (width - slots * cell_width))


def main():
    parser = argparse.ArgumentParser(description="Preview the fixed noob bar")
    parser.add_argument("--no-color", action="store_true")
    args = parser.parse_args()
    color = sys.stdout.isatty() and not args.no_color
    samples = (86, 58, 18)
    print("     %s  %s  %s" % tuple("%-20s" % ("%d%% left" % value) for value in samples))
    print("     %s" % "  ".join(render_shape(SHAPE, value, color=color)
                                for value in samples))


if __name__ == "__main__":
    main()
