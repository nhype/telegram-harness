import json
import os
import stat
import subprocess
import sys
from pathlib import Path

TOOL = Path(__file__).resolve().parents[1] / "harness_setup.py"


def run(*args, env=None):
    return subprocess.run([sys.executable, str(TOOL), *args], capture_output=True, text=True,
                          env={**os.environ, **(env or {})})


def test_env_copies_missing_keys_silently(tmp_path):
    (tmp_path / ".env").write_text("KEEP=1\n")
    r = run("env", "--home", str(tmp_path), "TELEGRAM_BOT_TOKEN", "KEEP",
            env={"TELEGRAM_BOT_TOKEN": "123:secret", "KEEP": "2"})
    assert r.returncode == 0 and "secret" not in r.stdout + r.stderr
    text = (tmp_path / ".env").read_text()
    assert "TELEGRAM_BOT_TOKEN=123:secret" in text and "KEEP=1" in text and "KEEP=2" not in text
    assert stat.S_IMODE((tmp_path / ".env").stat().st_mode) == 0o600


def test_env_force_replaces_value(tmp_path):
    (tmp_path / ".env").write_text("KEEP=1\n")
    assert run("env", "--home", str(tmp_path), "--force", "KEEP", env={"KEEP": "2"}).returncode == 0
    assert (tmp_path / ".env").read_text() == "KEEP=2\n"


def test_env_refuses_unset_key(tmp_path):
    r = run("env", "--home", str(tmp_path), "NOPE_NOT_SET", env={"NOPE_NOT_SET": ""})
    assert r.returncode == 2 and "NOPE_NOT_SET" in r.stderr
    assert not (tmp_path / ".env").exists()


def test_pipeline_written_private_and_valid(tmp_path):
    r = run("pipeline", "--home", str(tmp_path), "--profile", "acme", "--project", "Acme",
            "--repo", "/srv/acme", "--owner", "111111111")
    assert r.returncode == 0, r.stderr
    path = tmp_path / "herdr-pipeline.json"
    data = json.loads(path.read_text())
    assert data["cwd_prefixes"] == ["/srv/acme"] and data["route"] == "herdr-agent-events"
    assert data["chat_id"] == data["owner_user_id"] == "111111111"
    assert data["approval_tag"] == "[Acme Herdr approval]"
    assert data["sibling_prefix"] is False
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_pipeline_refuses_relative_repo_and_bad_owner(tmp_path):
    assert run("pipeline", "--home", str(tmp_path), "--profile", "a", "--project", "A",
               "--repo", "srv/a", "--owner", "123").returncode == 2
    assert run("pipeline", "--home", str(tmp_path), "--profile", "a", "--project", "A",
               "--repo", "/srv/a", "--owner", "me").returncode == 2
    assert not (tmp_path / "herdr-pipeline.json").exists()


def test_check_pipeline_reports_valid_and_invalid(tmp_path):
    assert run("check-pipeline", "--home", str(tmp_path)).returncode == 2
    run("pipeline", "--home", str(tmp_path), "--profile", "acme", "--project", "Acme",
        "--repo", "/srv/acme", "--owner", "111111111")
    ok = run("check-pipeline", "--home", str(tmp_path))
    assert ok.returncode == 0 and "Acme" in ok.stdout
    (tmp_path / "herdr-pipeline.json").write_text("{}")
    assert run("check-pipeline", "--home", str(tmp_path)).returncode == 2


def test_skills_render_then_keep_user_edits(tmp_path):
    src = tmp_path / "src" / "harness-controller"
    src.mkdir(parents=True)
    (src / "SKILL.md").write_text("project {{PROJECT}} at {{REPO}}\n")
    home = tmp_path / "home"
    args = ("skills", "--home", str(home), "--src", str(tmp_path / "src"),
            "--var", "PROJECT=Acme", "--var", "REPO=/srv/acme")
    assert run(*args).returncode == 0
    out = home / "skills" / "harness-controller" / "SKILL.md"
    assert out.read_text() == "project Acme at /srv/acme\n"
    (src / "SKILL.md").write_text("v2 {{PROJECT}}\n")
    assert run(*args).returncode == 0
    assert out.read_text() == "v2 Acme\n"  # an untouched copy is upgraded
    out.write_text("my notes\n")
    r = run(*args)
    assert r.returncode == 0 and "kept" in r.stdout and out.read_text() == "my notes\n"


def test_skills_refuse_unrendered_placeholder(tmp_path):
    src = tmp_path / "src" / "s"
    src.mkdir(parents=True)
    (src / "SKILL.md").write_text("{{MISSING}}\n")
    r = run("skills", "--home", str(tmp_path / "h"), "--src", str(tmp_path / "src"))
    assert r.returncode == 2 and "MISSING" in r.stderr
    assert not (tmp_path / "h" / "skills" / "s").exists()
