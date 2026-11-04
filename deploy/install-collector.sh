#!/usr/bin/env bash
# Copy the collector to a host and start it as a user service there. Usage: deploy/install-collector.sh HOST
# Touches only ~/alm-collector, ~/.config/systemd/user/alm-collector.service and that user's systemd on HOST.
# Remove with: deploy/install-collector.sh HOST --remove
set -euo pipefail
host=${1:?usage: install-collector.sh HOST [--remove]}
here=$(cd "$(dirname "$0")/.." && pwd)
if [ "${2:-}" = "--remove" ]; then
  ssh "$host" 'systemctl --user disable --now alm-collector 2>/dev/null; rm -f ~/.config/systemd/user/alm-collector.service; systemctl --user daemon-reload; echo "kept ~/alm-collector and ~/.local/share/alm-collector (the store); delete by hand if unwanted"'
  exit 0
fi
ssh "$host" 'mkdir -p ~/alm-collector ~/.config/systemd/user'
scp -q "$here/alm_collector.py" "$here/tail.py" "$here/burst.py" "$host:alm-collector/"
scp -q "$here/deploy/alm-collector.service" "$host:.config/systemd/user/alm-collector.service"
ssh "$host" 'systemctl --user daemon-reload && systemctl --user enable alm-collector && systemctl --user restart alm-collector && sleep 3 && systemctl --user is-active alm-collector && python3 ~/alm-collector/alm_collector.py status'
