#!/usr/bin/env bash
# Set up ALM on the machine that shows the screen (a laptop, a Chromebook's Linux container, a mini PC).
#
#   ./install.sh                         python environment only, then prints how to try it
#   ./install.sh --service               also install and start the systemd user service
#   ./install.sh --service --collector myserver    ...and point it at a server running the collector
#   ./install.sh --check                 diagnose an existing install
#   ./install.sh --uninstall             stop and remove the service (keeps your data and settings)
#
# Options: --dev (test tools), --no-start (write the unit, do not touch systemd), --skip-venv.
# Nothing outside this directory is touched unless you pass --service.
set -euo pipefail

here=$(cd "$(dirname "$0")" && pwd)
cfg=${XDG_CONFIG_HOME:-$HOME/.config}
unit_dir=$cfg/systemd/user
env_file=$cfg/alm/alm.env
service=0 dev=0 start=1 venv=1 uninstall=0 check=0 collector=""

while [ $# -gt 0 ]; do
  case $1 in
    --service) service=1 ;;
    --collector) collector=${2:?--collector needs a host}; shift ;;
    --dev) dev=1 ;;
    --no-start) start=0 ;;
    --skip-venv) venv=0 ;;
    --uninstall) uninstall=1 ;;
    --check) check=1 ;;
    -h|--help) sed -n '2,11p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown option: $1 (try --help)" >&2; exit 2 ;;
  esac
  shift
done

say() { printf '%s\n' "$*"; }
die() { printf 'error: %s\n' "$*" >&2; exit 1; }

if [ $uninstall = 1 ]; then
  command -v systemctl >/dev/null && systemctl --user disable --now alm 2>/dev/null || true
  rm -f "$unit_dir/alm.service"
  command -v systemctl >/dev/null && systemctl --user daemon-reload 2>/dev/null || true
  say "service removed. Kept: $cfg/alm/ (settings), ~/.local/share/alm/ (history), $here/.venv"
  exit 0
fi

if [ $check = 1 ]; then
  [ -x "$here/.venv/bin/python" ] || die "no .venv yet: run ./install.sh first"
  exec "$here/.venv/bin/python" "$here/tools/doctor.py"
fi

# 1. Python
command -v python3 >/dev/null || die "python3 not found"
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' \
  || die "Python 3.10 or newer is needed (found $(python3 -V 2>&1))"

# 2. Virtual environment (Debian and Ubuntu often lack python3-venv: fall back to bootstrapping pip)
if [ $venv = 1 ]; then
  if [ ! -x "$here/.venv/bin/python" ]; then
    say "creating .venv"
    if ! python3 -m venv "$here/.venv" >/dev/null 2>&1; then
      rm -rf "$here/.venv"
      python3 -m venv --without-pip "$here/.venv" || die "could not create a virtual environment"
      say "python3-venv is missing; bootstrapping pip (needs network)"
      python3 - "$here/.venv/bin/python" <<'PY' || die "could not fetch get-pip.py; install python3-venv (apt install python3-venv) and rerun"
import subprocess, sys, tempfile, urllib.request
with tempfile.NamedTemporaryFile(suffix=".py") as f:
    f.write(urllib.request.urlopen("https://bootstrap.pypa.io/get-pip.py", timeout=60).read()); f.flush()
    subprocess.check_call([sys.argv[1], f.name, "-q"])
PY
    fi
  fi
  say "installing dependencies"
  "$here/.venv/bin/python" -m pip install -q -r "$here/requirements.txt"
  if [ $dev = 1 ]; then "$here/.venv/bin/python" -m pip install -q -r "$here/requirements-dev.txt"; fi
  "$here/.venv/bin/python" -c 'import textual, fastapi, uvicorn' || die "dependencies did not import"
fi

# 3. Service (only on request)
if [ $service = 1 ]; then
  mkdir -p "$cfg/alm" "$unit_dir"
  if [ ! -e "$env_file" ]; then
    ( umask 077; cp "$here/deploy/alm.env.example" "$env_file" )
    chmod 600 "$env_file"
    say "wrote $env_file (mode 600)"
  fi
  if [ -n "$collector" ]; then
    if grep -q '^ALM_COLLECTOR=' "$env_file"; then
      sed -i "s|^ALM_COLLECTOR=.*|ALM_COLLECTOR=$collector|" "$env_file"
    else
      printf 'ALM_COLLECTOR=%s\n' "$collector" >> "$env_file"
    fi
    say "settings: ALM_COLLECTOR=$collector"
  fi
  sed "s|@REPO@|$here|g" "$here/deploy/alm.service" > "$unit_dir/alm.service"
  say "wrote $unit_dir/alm.service"
  if [ $start = 1 ]; then
    systemctl --user daemon-reload
    systemctl --user enable alm >/dev/null 2>&1
    systemctl --user restart alm
    sleep 2
    systemctl --user is-active --quiet alm && say "service running" || say "service did not start: journalctl --user -u alm -n 30"
    if command -v loginctl >/dev/null && [ "$(loginctl show-user "$USER" -p Linger --value 2>/dev/null)" = no ]; then
      say "note: run 'loginctl enable-linger $USER' so it keeps running after you log out"
    fi
  fi
fi

say ""
say "ALM is ready in $here"
say "  try it now (demo data):   .venv/bin/python main.py --web-only      then open http://localhost:8000/"
say "  terminal view instead:    .venv/bin/python main.py"
[ $service = 1 ] || say "  run it as a service:      ./install.sh --service [--collector HOST]"
say "  watch a server:           deploy/install-collector.sh HOST, then ./install.sh --service --collector HOST"
say "  diagnose:                 ./install.sh --check"
