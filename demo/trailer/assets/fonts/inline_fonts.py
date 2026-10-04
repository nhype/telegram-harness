#!/usr/bin/env python3
"""Inline fonts.css into index.html between the fonts:begin / fonts:end markers (idempotent)."""
import re
from pathlib import Path

HERE = Path(__file__).resolve().parent
INDEX = HERE.parent.parent / "index.html"

css = (HERE / "fonts.css").read_text(encoding="utf-8")
css = re.sub(r"url\(([\w.-]+\.woff2)\)", r'url("assets/fonts/\1")', css)
html = INDEX.read_text(encoding="utf-8")
block = "/* fonts:begin */\n" + css + "      /* fonts:end */"
html, n = re.subn(r"/\* fonts:begin \*/.*?/\* fonts:end \*/", lambda _: block, html, flags=re.S)
assert n == 1, "markers not found"
INDEX.write_text(html, encoding="utf-8")
print("inlined", css.count("@font-face"), "font faces")
