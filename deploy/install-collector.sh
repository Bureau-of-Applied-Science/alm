#!/usr/bin/env bash
# Install the ALM collector on the server you want to watch. It keeps the record there, so your screen is
# only a viewer and a sleeping laptop loses nothing.
#
#   deploy/install-collector.sh HOST                  detect the logs on HOST and install
#   deploy/install-collector.sh HOST --file PATH:PARSER[:sudo] ...   choose the log files yourself
#   deploy/install-collector.sh HOST --dry-run        show what would be installed, change nothing
#   deploy/install-collector.sh HOST --remove         stop and remove the service (keeps the stored data)
#
# HOST is an ssh alias with key authentication. Parsers: caddy, fail2ban, plain. `:sudo` reads a file through
# `sudo -n tail`, for logs your user cannot read. Options: --no-detect (only the --file ones), --no-journal.
# Touches only ~/alm-collector, ~/.config/systemd/user/alm-collector.service and that user's systemd on HOST.
# Needs Python 3.10+ on HOST and `loginctl enable-linger` for the user there (it tells you if missing).
set -euo pipefail

here=$(cd "$(dirname "$0")/.." && pwd)
host=${1:-}
case $host in ""|-h|--help) sed -n '2,13p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;; esac
shift
files=() detect=1 journal=1 dry=0 remove=0
while [ $# -gt 0 ]; do
  case $1 in
    --file) files+=("${2:?--file needs PATH:PARSER[:sudo]}"); shift ;;
    --no-detect) detect=0 ;;
    --no-journal) journal=0 ;;
    --dry-run) dry=1 ;;
    --remove) remove=1 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
  shift
done
die() { printf 'error: %s\n' "$*" >&2; exit 1; }
# One shared connection for every step: servers commonly rate-limit new ssh logins (ufw LIMIT, MaxStartups),
# and a script that logs in a dozen times in a few seconds gets refused ("Connection closed").
ctl=$(mktemp -d "${TMPDIR:-/tmp}/alm-ssh.XXXXXX")
sshopts=(-o BatchMode=yes -o ConnectTimeout=10 -o ControlMaster=auto -o "ControlPath=$ctl/c" -o ControlPersist=60)
trap 'ssh -O exit "${sshopts[@]}" "$host" 2>/dev/null || true; rm -rf "$ctl"' EXIT
rssh() { ssh -T "${sshopts[@]}" "$host" "$@"; }

if [ $remove = 1 ]; then
  rssh 'systemctl --user disable --now alm-collector 2>/dev/null; rm -f ~/.config/systemd/user/alm-collector.service; systemctl --user daemon-reload; echo "removed. Kept ~/alm-collector and ~/.local/share/alm-collector (the stored record); delete by hand if unwanted"'
  exit 0
fi

python=/usr/bin/python3
if [ $dry = 0 ]; then
  rssh true 2>/dev/null || die "cannot ssh to $host with a key (BatchMode). Try: ssh-copy-id $host"
  python=$(rssh 'command -v python3' || true)
  [ -n "$python" ] || die "python3 not found on $host"
  rssh "$python -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)'" \
    || die "$host needs Python 3.10 or newer ($(rssh "$python -V" 2>&1))"
fi

# Which logs? Detect the common ones the user can read (or read with passwordless sudo).
if [ $detect = 1 ] && [ $dry = 0 ]; then
  found=$(rssh 'for f in /var/log/fail2ban.log; do [ -e "$f" ] && { [ -r "$f" ] && echo "$f:fail2ban" || { sudo -n true 2>/dev/null && echo "$f:fail2ban:sudo"; }; }; done
                for f in /var/log/caddy/*.log; do [ -e "$f" ] && { [ -r "$f" ] && echo "$f:caddy" || { sudo -n true 2>/dev/null && echo "$f:caddy:sudo"; }; }; done; true')
  while IFS= read -r line; do [ -n "$line" ] && files+=("$line"); done <<< "$found"
fi
args=""
[ $journal = 1 ] || args="--no-journal"
for f in ${files[@]+"${files[@]}"}; do args="$args --file $f"; done
args=${args# }
echo "host:    $host ($python)"
echo "sources: journal$([ $journal = 1 ] || echo ' (off)')${files[*]:+, ${files[*]}}"
[ ${#files[@]} -gt 0 ] || echo "         (no log files found; add some with --file PATH:PARSER[:sudo])"

unit=$(sed -e "s|@PYTHON@|$python|" -e "s|@ARGS@|$args|" "$here/deploy/alm-collector.service")
if [ $dry = 1 ]; then
  echo "--- would install on $host: ~/alm-collector/{alm_collector.py,tail.py,burst.py} and ~/.config/systemd/user/alm-collector.service"
  echo "$unit"
  exit 0
fi

rssh 'mkdir -p ~/alm-collector ~/.config/systemd/user'
scp -q "${sshopts[@]}" "$here/alm_collector.py" "$here/tail.py" "$here/burst.py" "$host:alm-collector/"
printf '%s\n' "$unit" | rssh 'cat > ~/.config/systemd/user/alm-collector.service'
rssh 'systemctl --user daemon-reload && systemctl --user enable alm-collector >/dev/null 2>&1 && systemctl --user restart alm-collector' \
  || die "could not start the service (is there a user systemd session? try: loginctl enable-linger \$USER on $host)"
sleep 3
rssh 'systemctl --user is-active alm-collector' >/dev/null || die "the service is not active: ssh $host journalctl --user -u alm-collector -n 30"
rssh "$python ~/alm-collector/alm_collector.py status"
if [ "$(rssh 'loginctl show-user "$USER" -p Linger --value 2>/dev/null' || true)" = no ]; then
  echo "note: run 'loginctl enable-linger \$USER' on $host so the collector survives your logging out"
fi
echo "done. Point ALM at it: ./install.sh --service --collector $host"
