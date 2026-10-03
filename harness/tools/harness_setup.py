#!/usr/bin/env python3
"""Profile file work for install.sh: .env merge, pipeline config, skill rendering.

Secret values travel only through the environment and are never printed.
Each command exits 0 on success and 2 (with "error: ..." on stderr) on a refusal.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
from herdr_event_bridge import PIPELINE_CONFIG, load_pipeline_config  # noqa: E402

MARKER = ".harness-rendered"
PLACEHOLDER = re.compile(r"\{\{([A-Z_]+)\}\}")
OWNER_RE = re.compile(r"^-?\d{3,20}$")


class Refused(Exception):
    pass


def _write_private(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def cmd_env(home: Path, keys: list[str], force: bool) -> str:
    """Copy KEY values from the environment into home/.env (missing keys only unless force)."""
    path = home / ".env"
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    present = {ln.split("=", 1)[0].strip(): i for i, ln in enumerate(lines)
               if "=" in ln and not ln.lstrip().startswith("#")}
    for key in keys:  # validate everything before touching the file
        value = os.environ.get(key, "")
        if not value:
            raise Refused(f"{key} is not set in the environment")
        if "\n" in value:
            raise Refused(f"{key} contains a newline")
    changed = []
    for key in keys:
        if key in present and not force:
            continue
        entry = f"{key}={os.environ[key]}"
        if key in present:
            lines[present[key]] = entry
        else:
            lines.append(entry)
        changed.append(key)
    _write_private(path, "\n".join(lines) + "\n")
    return f"env: set {', '.join(changed) or 'nothing (all present)'}"


def cmd_pipeline(home: Path, profile: str, project: str, repos: list[str], sibling: bool,
                 owner: str, route: str) -> str:
    if not OWNER_RE.fullmatch(owner):
        raise Refused("--owner must be a numeric Telegram user id")
    if not repos or not all(r.startswith("/") for r in repos):
        raise Refused("--repo must be an absolute path (repeatable)")
    data = {"profile": profile, "project": project, "cwd_prefixes": repos, "sibling_prefix": sibling,
            "route": route, "chat_id": owner, "owner_user_id": owner,
            "approval_tag": f"[{project} Herdr approval]"}
    path = home / PIPELINE_CONFIG
    _write_private(path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    if load_pipeline_config(path) is None:
        path.unlink()
        raise Refused("pipeline config failed validation")
    return f"pipeline: wrote {path}"


def _render(text: str, values: dict[str, str]) -> str:
    missing = sorted({m for m in PLACEHOLDER.findall(text) if m not in values})
    if missing:
        raise Refused(f"unrendered placeholders: {', '.join(missing)}")
    return PLACEHOLDER.sub(lambda m: values[m.group(1)], text)


def _digest(folder: Path) -> str:
    h = hashlib.sha256()
    for f in sorted(p for p in folder.rglob("*") if p.is_file() and p.name != MARKER):
        h.update(str(f.relative_to(folder)).encode() + b"\0" + f.read_bytes())
    return h.hexdigest()


def cmd_skills(home: Path, src: Path, values: dict[str, str]) -> str:
    """Render each src/<skill>/ into home/skills/<skill>/, keeping copies the user edited."""
    plans = []
    for skill in sorted(p for p in src.iterdir() if p.is_dir()):
        rendered = {f.relative_to(skill): _render(f.read_text(encoding="utf-8"), values)
                    for f in skill.rglob("*") if f.is_file()}
        plans.append((skill.name, rendered))
    report = []
    for name, rendered in plans:
        dest = home / "skills" / name
        marker = dest / MARKER
        if dest.exists() and (not marker.exists() or marker.read_text().strip() != _digest(dest)):
            report.append(f"kept {name} (edited)")
            continue
        for rel, text in rendered.items():
            (dest / rel).parent.mkdir(parents=True, exist_ok=True)
            (dest / rel).write_text(text, encoding="utf-8")
        marker.write_text(_digest(dest) + "\n")
        report.append(f"rendered {name}")
    return "skills: " + "; ".join(report)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    env = sub.add_parser("env", help="copy KEYs from the environment into <home>/.env")
    env.add_argument("--home", required=True)
    env.add_argument("--force", action="store_true")
    env.add_argument("keys", nargs="+")
    pipe = sub.add_parser("pipeline", help="write <home>/herdr-pipeline.json")
    for flag in ("--home", "--profile", "--project", "--owner"):
        pipe.add_argument(flag, required=True)
    pipe.add_argument("--repo", action="append", default=[])
    pipe.add_argument("--sibling", action="store_true")
    pipe.add_argument("--route", default="herdr-agent-events")
    skills = sub.add_parser("skills", help="render skills into <home>/skills")
    skills.add_argument("--home", required=True)
    skills.add_argument("--src", required=True)
    skills.add_argument("--var", action="append", default=[])
    args = ap.parse_args(argv)
    try:
        home = Path(args.home).expanduser()
        if args.cmd == "env":
            print(cmd_env(home, args.keys, args.force))
        elif args.cmd == "pipeline":
            print(cmd_pipeline(home, args.profile, args.project, args.repo, args.sibling, args.owner, args.route))
        else:
            values = dict(v.split("=", 1) for v in args.var if "=" in v)
            print(cmd_skills(home, Path(args.src), values))
    except Refused as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
