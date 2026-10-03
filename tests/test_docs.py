"""Relative links in the docs point at files that exist."""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LINK = re.compile(r"\]\(([^)#:\s]+)(?:#[^)]*)?\)")


def test_relative_links_resolve():
    broken = []
    for doc in [*ROOT.glob("*.md"), *ROOT.glob("docs/*.md")]:
        for target in LINK.findall(doc.read_text(encoding="utf-8")):
            if not (doc.parent / target).exists():
                broken.append(f"{doc.relative_to(ROOT)} -> {target}")
    assert not broken, broken
