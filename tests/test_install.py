"""install.sh, deploy/install-collector.sh (dry run) and tools/doctor.py: the first-run experience."""
import os
import stat
import subprocess
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))


@pytest.fixture
def sandbox(tmp_path):
    """A fake HOME and a fake systemctl first on PATH, so no test can touch the real user service."""
    home = tmp_path / "home"
    bindir = tmp_path / "bin"
    home.mkdir()
    bindir.mkdir()
    calls = tmp_path / "systemctl.calls"
    fake = bindir / "systemctl"
    fake.write_text(f'#!/bin/sh\necho "$@" >> {calls}\nexit 0\n')
    fake.chmod(0o755)
    env = {**os.environ, "HOME": str(home), "XDG_CONFIG_HOME": str(home / ".config"),
           "PATH": f"{bindir}:{os.environ['PATH']}"}
    env.pop("ALM_COLLECTOR", None)
    return env, home, calls


def sh(args, env, cwd=ROOT):
    return subprocess.run(args, env=env, cwd=cwd, capture_output=True, text=True, timeout=60)


def test_installer_writes_unit_and_settings_without_touching_systemd(sandbox):
    env, home, calls = sandbox
    r = sh(["./install.sh", "--skip-venv", "--service", "--no-start", "--collector", "myserver"], env)
    assert r.returncode == 0, r.stderr
    envf = home / ".config/alm/alm.env"
    assert stat.S_IMODE(envf.stat().st_mode) == 0o600
    assert "ALM_COLLECTOR=myserver" in envf.read_text()
    unit = (home / ".config/systemd/user/alm.service").read_text()
    assert f"WorkingDirectory={ROOT}" in unit and f"{ROOT}/.venv/bin/python main.py" in unit and "@REPO@" not in unit
    assert "EnvironmentFile=-%h/.config/alm/alm.env" in unit
    assert not calls.exists()                       # --no-start never calls systemctl


def test_installer_never_overwrites_settings_and_can_repoint_the_host(sandbox):
    env, home, _ = sandbox
    envf = home / ".config/alm/alm.env"
    envf.parent.mkdir(parents=True)
    envf.write_text("ALM_TOKEN=keepme\nALM_COLLECTOR=old\n")
    assert sh(["./install.sh", "--skip-venv", "--service", "--no-start", "--collector", "new"], env).returncode == 0
    assert envf.read_text() == "ALM_TOKEN=keepme\nALM_COLLECTOR=new\n"
    assert sh(["./install.sh", "--skip-venv", "--service", "--no-start"], env).returncode == 0
    assert "ALM_TOKEN=keepme" in envf.read_text()


def test_installer_without_service_touches_nothing_outside_the_repo(sandbox):
    env, home, calls = sandbox
    r = sh(["./install.sh", "--skip-venv"], env)
    assert r.returncode == 0 and "./install.sh --service" in r.stdout
    assert not (home / ".config").exists() and not calls.exists()


def test_uninstall_removes_only_the_unit(sandbox):
    env, home, calls = sandbox
    sh(["./install.sh", "--skip-venv", "--service", "--no-start"], env)
    r = sh(["./install.sh", "--uninstall"], env)
    assert r.returncode == 0
    assert not (home / ".config/systemd/user/alm.service").exists() and (home / ".config/alm/alm.env").exists()
    assert "disable --now alm" in calls.read_text()


def test_installer_rejects_unknown_options(sandbox):
    env, _, _ = sandbox
    assert sh(["./install.sh", "--nope"], env).returncode == 2


def test_collector_dry_run_renders_the_unit_and_changes_nothing(sandbox):
    env, _, calls = sandbox
    r = sh(["deploy/install-collector.sh", "myserver", "--dry-run", "--file", "/var/log/x.log:plain",
            "--file", "/var/log/caddy/access.log:caddy:sudo"], env)
    assert r.returncode == 0, r.stderr
    assert "run --file /var/log/x.log:plain --file /var/log/caddy/access.log:caddy:sudo" in r.stdout
    assert "@PYTHON@" not in r.stdout and "@ARGS@" not in r.stdout
    r2 = sh(["deploy/install-collector.sh", "myserver", "--dry-run", "--no-journal"], env)
    assert "run --no-journal" in r2.stdout and not calls.exists()


def test_the_env_example_is_all_comments_so_a_fresh_install_runs_the_demo():
    lines = [l for l in open(os.path.join(ROOT, "deploy/alm.env.example")).read().splitlines() if l.strip()]
    assert all(l.startswith("#") for l in lines)


def test_doctor_parses_the_settings_file(tmp_path):
    import doctor
    p = tmp_path / "alm.env"
    p.write_text("# comment\nALM_COLLECTOR=a,b\nALM_TOKEN='s3'\n\nbroken line\n")
    assert doctor.read_env(p) == {"ALM_COLLECTOR": "a,b", "ALM_TOKEN": "s3"}
    assert doctor.read_env(tmp_path / "missing") == {}


def test_collectors_come_from_the_flag_else_the_environment():
    from main import resolve_collectors
    assert resolve_collectors(["x"], {"ALM_COLLECTOR": "y"}) == ["x"]
    assert resolve_collectors([], {"ALM_COLLECTOR": "y, z,"}) == ["y", "z"]
    assert resolve_collectors([], {}) == []
