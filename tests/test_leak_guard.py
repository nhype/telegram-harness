"""The public tree carries no trace of the private deployment it was extracted from.

Tokens are stored as (length, SHA-256) so this file does not itself contain them, and every
substring of that length is checked, so a name glued into a handle, path or id is caught too.
Failures report file names and counts only, never the matched text.
"""
import hashlib
import re
import subprocess
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FORBIDDEN = {
    (7, "99a18e58dea3c0c2a4c1b114d70231176220a4022e384bcec8089daa3f9b23e0"),  # private-1
    (10, "d49af6a6b90de387132c17250efd0d642d20dfd955074855c9ea94026c74cd44"),  # private-2
    (9, "c2e405ec218ffa59db21c72b0352fc717613ff17883a10930eeea78856054dfe"),  # private-3
    (3, "9e66a118b9a0fb8cada5eb0f357806a21cc067fa4b9d9f76eb9773a24e022438"),  # private-4
    (6, "61351ec6a9941106d584d91a7f319a20520b9f4cc8cdd94cef5ba09c3201c9af"),  # private-5
    (9, "f1d1b44aaa391c32a711ad8a22584b4556219dbaa3204f5de84d555ea15320d8"),  # private-6
    (10, "62da8b980b206bb225fdb0004baf5b3d8a9cd19183ca49164578dad36c1e93b5"),  # private-7
    (10, "1b76c628b839654b8899ac76c8651a888c68736482e0717ecd20312bd1079bee"),  # private-8
    (12, "d40d60fc34f7902e7ac9ded8122eb811f1318eae3077fe8eb09835a598887d66"),  # private-9
    (8, "02106a176ffd9bb35c1852119c0884d50e5ed07c24c0dae315e3cc073bc24150"),  # private-10
}
MIN_EMBEDDED = 5  # shorter tokens would match inside ordinary words: whole words only
SECRET_SHAPES = [
    re.compile(r"\b\d{8,10}:[A-Za-z0-9_-]{35}\b"),  # Telegram bot token
    re.compile(r"\bgh[opsu]_[A-Za-z0-9]{30,}\b"),  # GitHub tokens
    re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"),  # API keys
]
CODE_GLOBS = ("harness/**/*.py", "install.sh", "bin/*", "lib/*", "templates/**/*")
RUN = re.compile(r"[a-z0-9]+")


def tracked_files():
    out = subprocess.run(["git", "ls-files", "-co", "--exclude-standard"], cwd=ROOT,
                         capture_output=True, text=True, check=True).stdout.split()
    return [ROOT / p for p in out if (ROOT / p).is_file()]


def _sha(word: str) -> str:
    return hashlib.sha256(word.encode()).hexdigest()


def leaks(text: str, forbidden=FORBIDDEN) -> int:
    """Number of private tokens and secret-shaped strings in text."""
    by_length = defaultdict(set)
    for length, digest in forbidden:
        by_length[length].add(digest)
    hits = 0
    for run in set(RUN.findall(text.lower())):
        for length, digests in by_length.items():
            if length > len(run):
                continue
            if length < MIN_EMBEDDED:
                hits += len(run) == length and _sha(run) in digests
                continue
            hits += any(_sha(run[i:i + length]) in digests for i in range(len(run) - length + 1))
    return hits + sum(len(rx.findall(text)) for rx in SECRET_SHAPES)


def test_no_private_tokens_or_secrets():
    bad = {}
    for path in tracked_files():
        count = leaks(path.read_text(encoding="utf-8", errors="ignore"))
        if count:
            bad[str(path.relative_to(ROOT))] = count
    assert not bad, f"private tokens or secrets found (file: count): {bad}"


def test_code_has_no_root_paths():
    bad = [str(p.relative_to(ROOT)) for g in CODE_GLOBS for p in ROOT.glob(g)
           if p.is_file() and "/root/" in p.read_text(encoding="utf-8", errors="ignore")]
    assert not bad, f"/root/ paths in code: {bad}"


def test_guard_catches_a_planted_token():
    planted = "123456789:" + "A" * 35
    assert leaks(f"token={planted}")


def test_guard_catches_names_embedded_in_longer_words():
    table = {(10, _sha("secretword")), (9, _sha("123456789")), (3, _sha("abc"))}
    for text in ("@MySecretwordBot", "secretword2", "/srv/xsecretwordy/path", "id123456789", "x abc y"):
        assert leaks(text, table), text
    assert not leaks("abcdef abcabc", table)  # short tokens match whole words only
