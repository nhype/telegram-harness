#!/usr/bin/env python3
"""Download the trailer's web fonts (latin + cyrillic subsets) and write a local fonts.css.

Families are renamed to TH * so they never collide with the renderer's bundled copies (which may lack
Cyrillic). All four are SIL Open Font License; their license texts are saved next to the fonts.
Run once: python3 fetch_fonts.py
"""
import re
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
FAMILIES = {  # Google family -> (css query, local name, OFL path in google/fonts)
    "Cormorant Garamond": ("Cormorant+Garamond:ital,wght@0,500;0,600;1,500", "TH Serif", "ofl/cormorantgaramond"),
    "Oswald": ("Oswald:wght@500;700", "TH Display", "ofl/oswald"),
    "Inter": ("Inter:wght@400;600;700", "TH Sans", "ofl/inter"),
    "JetBrains Mono": ("JetBrains+Mono:wght@400;700", "TH Mono", "ofl/jetbrainsmono"),
}
SUBSETS = {"latin", "cyrillic"}


def get(url):
    return urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": UA}), timeout=30).read()


def main():
    out = []
    for family, (query, local, ofl) in FAMILIES.items():
        css = get(f"https://fonts.googleapis.com/css2?family={query}&display=block").decode()
        for subset, block in re.findall(r"/\* ([a-z-]+) \*/\s*(@font-face \{.*?\})", css, re.S):
            if subset not in SUBSETS:
                continue
            style = re.search(r"font-style: (\w+)", block).group(1)
            weight = re.search(r"font-weight: (\d+)", block).group(1)
            url = re.search(r"url\((https://[^)]+\.woff2)\)", block).group(1)
            name = f"{local.replace(' ', '')}-{weight}{'i' if style == 'italic' else ''}-{subset}.woff2"
            (HERE / name).write_bytes(get(url))
            out.append(block.replace(url, name).replace(f"'{family}'", f"'{local}'"))
        (HERE / f"OFL-{local.replace(' ', '')}.txt").write_bytes(
            get(f"https://raw.githubusercontent.com/google/fonts/main/{ofl}/OFL.txt"))
        print(f"{family} -> {local}")
    (HERE / "fonts.css").write_text("\n".join(out) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
