#!/usr/bin/env python3
"""Render the README demo (docs/assets/demo-dark.svg, demo-light.svg) from one layout.

A hand-built SVG "screenshot" of the harness at work: the owner's Telegram chat on the left, the
agents' Herdr panes and the bridge log on the right. Lines fade in one after another like a terminal
recording, then stay. Colors follow GitHub's Primer palette so it sits natively in both themes.

Run: python3 docs/assets/make_demo.py
"""
from html import escape
from pathlib import Path

HERE = Path(__file__).resolve().parent
W, H = 1200, 600
SANS = "-apple-system, BlinkMacSystemFont, 'Segoe UI', 'Noto Sans', Helvetica, Arial, sans-serif"
MONO = "ui-monospace, SFMono-Regular, 'SF Mono', Menlo, Consolas, 'Liberation Mono', monospace"

THEMES = {
    "dark": dict(bg="#0d1117", card="#161b22", card2="#0d1117", border="#30363d", text="#e6edf3",
                 muted="#8b949e", link="#58a6ff", blue="#1f6feb", bluetext="#ffffff", bubble="#21262d", green="#3fb950",
                 purple="#a371f7", yellow="#d29922", red="#f85149", chip="#21262d"),
    "light": dict(bg="#ffffff", card="#f6f8fa", card2="#ffffff", border="#d0d7de", text="#1f2328",
                  muted="#656d76", link="#0969da", blue="#0969da", bluetext="#ffffff", bubble="#eaeef2", green="#1a7f37",
                  purple="#8250df", yellow="#9a6700", red="#cf222e", chip="#eaeef2"),
}

step = 0


def appear():
    """Next element in the fade-in sequence."""
    global step
    step += 1
    return f'class="a" style="animation-delay:{0.3 + step * 0.2:.2f}s"'


def text(x, y, s, size, fill, family=SANS, weight=400, anchor="start", extra=""):
    return (f'<text x="{x}" y="{y}" font-family="{family}" font-size="{size}" font-weight="{weight}" '
            f'fill="{fill}" text-anchor="{anchor}" {extra}>{s}</text>')


def spans(parts, x, y, size, family=MONO):
    """One monospace line made of (text, color) parts."""
    inner = "".join(f'<tspan fill="{c}">{escape(t)}</tspan>' for t, c in parts)
    return f'<text x="{x}" y="{y}" font-family="{family}" font-size="{size}" xml:space="preserve">{inner}</text>'


def bubble(x, y, w, lines, time, mine, c):
    h = 30 + 21 * len(lines)
    fill, ink = (c["blue"], c["bluetext"]) if mine else (c["bubble"], c["text"])
    body = "".join(text(x + 14, y + 26 + 21 * i, escape(line), 14.5, ink) for i, line in enumerate(lines))
    stamp = text(x + w - 12, y + h - 9, time, 11, ink if mine else c["muted"], anchor="end",
                 extra='opacity="0.75"' if mine else "")
    return (f'<g {appear()}><rect x="{x}" y="{y}" width="{w}" height="{h}" rx="14" fill="{fill}"/>'
            f'{body}{stamp}</g>'), h


def render(name):
    global step
    step = 0
    c = THEMES[name]
    out = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}" '
           f'role="img" aria-label="telegram-harness: a Telegram chat drives Claude Code agents in Herdr panes">',
           # "backwards": hidden only while waiting to appear, so a viewer without animation sees it all
           "<style>.a{animation:in .5s ease-out backwards}"
           "@keyframes in{from{opacity:0;transform:translateY(4px)}to{opacity:1;transform:none}}"
           "@media (prefers-reduced-motion:reduce){.a{animation:none}}</style>",
           f'<rect width="{W}" height="{H}" rx="16" fill="{c["bg"]}"/>']

    # Chat card -------------------------------------------------------------------------------
    cx, cy, cw, ch = 28, 28, 400, H - 56
    out.append(f'<rect x="{cx}" y="{cy}" width="{cw}" height="{ch}" rx="14" fill="{c["card"]}" stroke="{c["border"]}"/>')
    out.append(f'<circle cx="{cx + 36}" cy="{cy + 36}" r="16" fill="{c["blue"]}"/>')
    out.append(text(cx + 36, cy + 41, "H", 15, c["bluetext"], weight=700, anchor="middle"))
    out.append(text(cx + 62, cy + 33, "Harness bot", 15, c["text"], weight=600))
    out.append(text(cx + 62, cy + 51, "bot · your server", 12.5, c["muted"]))
    out.append(f'<line x1="{cx}" y1="{cy + 70}" x2="{cx + cw}" y2="{cy + 70}" stroke="{c["border"]}"/>')
    y = cy + 88
    for lines, time, mine, w in (
        (["Add CSV export to the reports", "page, ship it to production"], "20:40", True, 236),
        (["On it. Agent started, plan in", "openspec/changes/add-csv-export"], "20:41", False, 290),
        (["Plan reviewed, no blockers.", "Implementing the 9 tasks."], "20:58", False, 290),
        (["Deployed. Live check passed:", "export downloads, totals match.", "Merged into main ✓"], "21:52", False, 290),
        (["nice"], "21:53", True, 96),
    ):
        x = cx + cw - w - 18 if mine else cx + 18
        g, h = bubble(x, y, w, lines, time, mine, c)
        out.append(g)
        y += h + 12
    out.append(text(cx + cw / 2, cy + ch - 18, "asked you nothing in between", 12.5, c["muted"], anchor="middle",
                    extra=appear()))

    # Terminal card ---------------------------------------------------------------------------
    tx, ty, tw, th = cx + cw + 24, 28, W - (cx + cw + 24) - 28, H - 56
    out.append(f'<rect x="{tx}" y="{ty}" width="{tw}" height="{th}" rx="14" fill="{c["card"]}" stroke="{c["border"]}"/>')
    for i, dot in enumerate((c["red"], c["yellow"], c["green"])):
        out.append(f'<circle cx="{tx + 22 + i * 18}" cy="{ty + 22}" r="5.5" fill="{dot}"/>')
    out.append(text(tx + tw / 2, ty + 27, "herdr · default", 13, c["muted"], family=MONO, anchor="middle"))
    out.append(f'<line x1="{tx}" y1="{ty + 44}" x2="{tx + tw}" y2="{ty + 44}" stroke="{c["border"]}"/>')

    px, py, gap = tx + 16, ty + 60, 14
    pw, ph = (tw - 32 - gap) / 2, 272
    panes = (
        ("w1:p2", "author", [
            [("❯ ", c["muted"]), ("/opsx:apply add-csv-export", c["text"])],
            [("● ", c["purple"]), ("Read tasks.md, handoff.md", c["text"])],
            [("● ", c["purple"]), ("Edit ", c["text"]), ("reports/export.ts", c["link"]), (" +84", c["green"])],
            [("● ", c["purple"]), ("Edit ", c["text"]), ("reports/page.tsx", c["link"]), (" +31", c["green"])],
            [("● ", c["purple"]), ("npm test reports ", c["text"]), ("42 passed", c["green"])],
            [("● ", c["purple"]), ("./deploy.sh ", c["text"]), ("ok", c["green"])],
            [("● ", c["purple"]), ("make smoke ", c["text"]), ("PASS", c["green"])],
            [("● ", c["purple"]), ("/opsx:archive · push", c["text"])],
        ]),
        ("w1:p3", "reviewer", [
            [("❯ ", c["muted"]), ("review add-csv-export", c["text"])],
            [("  money        ", c["muted"]), ("ok", c["green"])],
            [("  privacy      ", c["muted"]), ("ok", c["green"])],
            [("  security     ", c["muted"]), ("ok", c["green"])],
            [("  data loss    ", c["muted"]), ("ok", c["green"])],
            [("  concurrency  ", c["muted"]), ("ok", c["green"])],
            [("", c["muted"])],
            [("  PASS ", c["green"]), ("· 0 blocking findings", c["text"])],
        ]),
    )
    for n, (pane, role, lines) in enumerate(panes):
        x = px + n * (pw + gap)
        out.append(f'<rect x="{x}" y="{py}" width="{pw}" height="{ph}" rx="10" fill="{c["card2"]}" stroke="{c["border"]}"/>')
        out.append(f'<rect x="{x + 12}" y="{py + 12}" width="{56}" height="22" rx="11" fill="{c["chip"]}"/>')
        out.append(text(x + 40, py + 27, pane, 12, c["muted"], family=MONO, anchor="middle"))
        out.append(text(x + 78, py + 28, f"claude · {role}", 13, c["text"], family=MONO, weight=600))
        for i, parts in enumerate(lines):
            out.append(f'<g {appear()}>{spans(parts, x + 14, py + 62 + i * 27, 13.5)}</g>')

    # Bridge log ------------------------------------------------------------------------------
    ly = py + ph + 16
    out.append(f'<rect x="{px}" y="{ly}" width="{tw - 32}" height="{th - (ly - ty) - 16}" rx="10" '
               f'fill="{c["card2"]}" stroke="{c["border"]}"/>')
    out.append(text(px + 14, ly + 24, "harness-bridge", 12.5, c["muted"], family=MONO, weight=600))
    log = (
        [("20:40:58 ", c["muted"]), ("event      ", c["link"]), ("task started · author in w1:p2", c["text"])],
        [("20:41:07 ", c["muted"]), ("event      ", c["link"]), ("author idle → controller", c["text"])],
        [("20:58:30 ", c["muted"]), ("controller ", c["purple"]), ("plan approved → /opsx:apply", c["text"])],
        [("21:31:44 ", c["muted"]), ("controller ", c["purple"]), ("review PASS → deploy + smoke", c["text"])],
        [("21:52:10 ", c["muted"]), ("watchdog   ", c["green"]), ("no stalls · 0 alerts · done", c["text"])],
    )
    for i, parts in enumerate(log):
        out.append(f'<g {appear()}>{spans(parts, px + 14, ly + 50 + i * 24, 13)}</g>')
    out.append("</svg>")
    (HERE / f"demo-{name}.svg").write_text("\n".join(out) + "\n", encoding="utf-8")


if __name__ == "__main__":
    for theme in THEMES:
        render(theme)
    print("wrote", ", ".join(f"demo-{t}.svg" for t in THEMES))
