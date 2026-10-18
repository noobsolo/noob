"""Build four static, sample-data reset-card design previews."""

from pathlib import Path


OUT = Path(__file__).parent
BG = "#0b0d0f"
INK = "#f1f5f7"
MINT = "#78edcf"
MUTED = "#a2a9b3"
DIM = "#79838e"
LINE = "#39454d"


def rect(x, y, w, h, fill, rx=0, stroke=None, sw=1):
    border = f' stroke="{stroke}" stroke-width="{sw}"' if stroke else ""
    return f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{rx}" fill="{fill}"{border}/>'


def line(x1, y1, x2, y2, color=LINE, sw=2):
    return f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" stroke="{color}" stroke-width="{sw}"/>'


def txt(x, y, value, size=24, color=INK, weight=600, spacing=0, anchor=None, opacity=None):
    extra = f' text-anchor="{anchor}"' if anchor else ""
    extra += f' opacity="{opacity}"' if opacity else ""
    return (f'<text x="{x}" y="{y}" fill="{color}" font-size="{size}" '
            f'font-weight="{weight}" letter-spacing="{spacing}"{extra}>{value}</text>')


def shell(title, body, footer_note="EXPIRY TIMES · LOCAL TIME UTC+8"):
    parts = [f'''<svg xmlns="http://www.w3.org/2000/svg" width="1080" height="1350" viewBox="0 0 1080 1350" role="img" aria-label="{title}">
<defs>
  <pattern id="grid" width="52" height="52" patternUnits="userSpaceOnUse"><rect x="25" y="25" width="4" height="4" fill="#20282e"/></pattern>
  <filter id="glow" x="-40%" y="-40%" width="180%" height="180%"><feGaussianBlur stdDeviation="12"/></filter>
  <style>text {{ font-family: Menlo, Monaco, 'Courier New', monospace; }}</style>
</defs>''', rect(0, 0, 1080, 1350, BG), rect(0, 0, 1080, 1350, "url(#grid)"),
        txt(88, 101, "✱  NOOBSOLO", 27, MINT, 800, 2),
        txt(992, 101, "RESET CARD / 09 OCT 2026", 18, MUTED, 700, 1, "end"),
        *body,
        line(88, 1230, 992, 1230),
        txt(88, 1270, "DESIGN PREVIEW  ·  SAMPLE DATA", 17, DIM, 600, 1),
        txt(992, 1270, footer_note, 17, DIM, 600, 1, "end"), "</svg>"]
    return "\n".join(parts) + "\n"


def signal():
    p = [txt(88, 228, "CODEX RESET", 26, MUTED, 700, 6),
         txt(82, 520, "03", 330, MINT, 800, -28),
         txt(570, 394, "AVAILABLE", 47, INK, 800, 1),
         txt(572, 447, "RESET CREDITS", 23, MUTED, 600, 3),
         line(570, 481, 992, 481, MINT, 4),
         txt(88, 635, "EXPIRY SCHEDULE", 21, MUTED, 700, 4),
         txt(992, 635, "SOONEST FIRST", 18, DIM, 600, 2, "end")]
    rows = [(760, "01", "10 OCT", "09:00"), (910, "02", "13 OCT", "18:30"),
            (1060, "03", "18 OCT", "09:00")]
    for y, n, date, time in rows:
        p.extend([line(88, y-70, 992, y-70), txt(88, y, n, 22, MINT, 700, 1),
                  txt(173, y, date, 41, INK, 700, 0),
                  txt(992, y, time, 57, INK, 800, -2, "end")])
    return shell("Reset card demo 1, Signal", p)


def ledger():
    p = [txt(88, 236, "RESET / LEDGER", 25, MUTED, 700, 5),
         txt(88, 445, "03", 225, INK, 800, -18),
         txt(486, 350, "CREDITS", 47, MINT, 800, 1),
         txt(488, 401, "AVAILABLE NOW", 22, MUTED, 700, 3),
         rect(88, 516, 904, 594, "#11181b", 10, LINE, 2),
         txt(128, 580, "#", 18, DIM, 700, 1),
         txt(233, 580, "EXPIRES ON", 18, DIM, 700, 2),
         txt(952, 580, "TIME", 18, DIM, 700, 2, "end")]
    rows = [(690, "01", "SAT / 10 OCT", "09:00"),
            (852, "02", "TUE / 13 OCT", "18:30"),
            (1014, "03", "SUN / 18 OCT", "09:00")]
    for y, n, date, time in rows:
        p.extend([line(128, y-65, 952, y-65, LINE, 1),
                  txt(128, y, n, 27, MINT, 700),
                  txt(233, y, date, 31, INK, 700),
                  txt(952, y, time, 45, INK, 800, -1, "end")])
    p += [txt(88, 1180, "THREE WINDOWS OPEN", 21, MINT, 700, 3),
          txt(992, 1180, "USE BEFORE EXPIRY ↗", 19, MUTED, 600, 1, "end")]
    return shell("Reset card demo 2, Ledger", p)


def windows():
    p = [txt(88, 226, "RESET WINDOWS", 26, MUTED, 700, 6),
         txt(88, 371, "3", 165, MINT, 800, -9),
         txt(246, 313, "AVAILABLE", 38, INK, 800, 1),
         txt(248, 362, "CREDITS  /  EXPIRES AT", 21, MUTED, 700, 2),
         line(88, 420, 992, 420, MINT, 4)]
    columns = [(88, "01", "10", "OCT", "09:00"),
               (396, "02", "13", "OCT", "18:30"),
               (704, "03", "18", "OCT", "09:00")]
    for x, n, day, month, time in columns:
        p.extend([rect(x, 490, 288, 554, "#12191c", 8, LINE, 2),
                  rect(x, 490, 288, 9, MINT),
                  txt(x+26, 556, "CREDIT " + n, 20, MINT, 700, 2),
                  txt(x+22, 795, day, 145, INK, 800, -12),
                  txt(x+26, 856, month, 30, MUTED, 700, 5),
                  line(x+26, 898, x+262, 898, LINE, 1),
                  txt(x+26, 985, time, 45, INK, 800, -2)])
    p.extend([txt(88, 1136, "01 — 03", 20, MINT, 700, 2),
              txt(992, 1136, "SORTED BY EXPIRY", 20, MUTED, 600, 2, "end")])
    return shell("Reset card demo 3, Windows", p)


def pulse():
    pink = "#e96091"
    p = [rect(88, 170, 904, 296, "#b00065", 10),
         txt(125, 230, "CODEX RESET", 23, "#ffffff", 800, 4),
         txt(123, 411, "03", 206, "#ffffff", 800, -17),
         txt(515, 323, "AVAILABLE", 45, "#ffffff", 800, 1),
         txt(518, 372, "CREDITS REMAINING", 21, "#ffe3ef", 700, 2),
         txt(88, 556, "EXPIRY TIMELINE", 23, MUTED, 700, 4),
         txt(992, 556, "LOCAL · UTC+8", 18, DIM, 600, 1, "end"),
         line(137, 647, 137, 1057, LINE, 3)]
    rows = [(661, "10 OCT", "09:00", "01"),
            (859, "13 OCT", "18:30", "02"),
            (1057, "18 OCT", "09:00", "03")]
    for y, date, time, n in rows:
        p.extend([rect(126, y-11, 22, 22, pink, 4),
                  txt(191, y+12, date, 37, INK, 700, 1),
                  txt(992, y+12, time, 50, INK, 800, -2, "end"),
                  txt(191, y+57, "RESET CREDIT  " + n, 17, DIM, 600, 2)])
    p.append(txt(88, 1178, "USE THEM BEFORE THE CLOCK RUNS OUT", 18, MUTED, 600, 1))
    return shell("Reset card demo 4, Pulse", p)


def main():
    for name, build in (("01-signal", signal), ("02-ledger", ledger),
                        ("03-windows", windows), ("04-pulse", pulse)):
        (OUT / (name + ".svg")).write_text(build(), encoding="utf-8")


if __name__ == "__main__":
    main()
