# ALM operations

How the live install is set up, how to run things, and how to debug. For how the code works see `ARCHITECTURE.md`.

## Reference install

| Thing | Value |
|---|---|
| Host | Chromebook, Crostini container (Debian 12, x86_64) |
| Code | `~/code/alm` (git checkout) |
| Python | `.venv/` in the repo (created with `venv --without-pip` + get-pip because `python3-venv` is missing) |
| Service | systemd **user** unit `alm`, installed from `deploy/alm-crostini.service` to `~/.config/systemd/user/alm.service`; enabled; user linger is on, so it survives logout |
| Command | `main.py --web-only --port 8000 --ssh myserver --ssh-file myserver:/var/log/caddy/access.log:caddy:sudo` |
| URL | `http://localhost:8000/` (Crostini forwards the port to ChromeOS); screensaver view `http://localhost:8000/?screensaver=1` |
| History | `~/.local/share/alm/history.db` (SQLite, WAL, 7 days, capped at 5M rows (`--max-rows`)) |
| Watched host | `myserver` (Ubuntu server), ssh alias with key auth, user `ubuntu`; Caddy log read with `sudo -n tail` |
| Extension | `extension/` loaded unpacked in ChromeOS Chrome from "Linux files" (code > alm > extension) |


## Everyday commands

```
systemctl --user status alm              # is it up
systemctl --user restart alm             # after any Python change
journalctl --user -u alm -f              # engine stderr/stdout
curl -s -o /dev/null -w '%{http_code}\n' http://localhost:8000/     # expect 200
curl -s http://localhost:8000/api/state | head -c 400               # snapshot (add ?token= if set)
.venv/bin/pytest                          # full suite (several minutes)
.venv/bin/pytest tests/test_engine.py tests/test_playback.py -q     # fast logic tests
```

Reinstalling the unit after editing `deploy/alm-crostini.service`:

```
cp deploy/alm-crostini.service ~/.config/systemd/user/alm.service
systemctl --user daemon-reload && systemctl --user restart alm
```

Systemd unit gotcha: comments must be on their own line (a trailing `# ...` after a setting is invalid). `KillMode=control-group` is deliberate: it kills the `ssh ... tail` children with the service, even after a hard kill.

## Running by hand (development)

```
.venv/bin/python main.py                                  # mock data, TUI + web on 127.0.0.1:8000
.venv/bin/python main.py --web-only --port 8001 --tail /tmp/alm.log      # a second instance on another port
python3 tools/fake_log.py /tmp/alm.log                   # feeds that file
.venv/bin/python main.py --web-only --journal             # local journald
.venv/bin/python main.py --web-only --no-history --ssh myserver   # live, nothing stored
```

Stop the service before binding port 8000 by hand, or use another port. A hand-run instance uses the **real** history DB unless you pass `--no-history` or `--db /tmp/x.db`.

## Flag reference (`main.py`)

| Flag | Default | Meaning |
|---|---|---|
| `--host` | 127.0.0.1 | bind address (warns if non-loopback without a token) |
| `--port` | 8000 | web port |
| `--no-web` / `--web-only` | | TUI only / no TUI |
| `--tail FILE` | | follow a local file (repeatable) |
| `--journal` | off | local journald |
| `--ssh HOST` | | remote journal + `/var/log/fail2ban.log` (repeatable) |
| `--ssh-file HOST:PATH:PARSER[:sudo]` | | one remote file; parsers `caddy`, `fail2ban`, `plain` |
| `--token` | `ALM_TOKEN` (`SPOT_TOKEN` still read) | shared secret for `/ws` and `/api/state` |
| `--db` | `~/.local/share/alm/history.db` | history file |
| `--no-history` | | no store, no timeline |
| `--keep-days` | 7 | retention |
| `--max-rows` | 5000000 | history row cap (about 1.5 GB), oldest dropped first |
| `--backfill HOURS` | 24 | journal backfill at start, 0 disables |

With none of `--tail/--journal/--ssh/--ssh-file` the engine runs in **mock mode** (fake entries, no history).

## Extension install and update

1. `chrome://extensions`, Developer mode on, Load unpacked, pick `extension/` from Linux files (if the picker cannot read it, copy the folder into My files; this was not needed so far).
2. After pulling or editing extension files, click the circular reload arrow on its card. The options page title shows the loaded version (currently v0.4.0).
3. Options: ALM address (`http://localhost:8000`), token if set, idle timeout in seconds (minimum 15), Enabled. **Test now** opens the saver, **Stop** closes it.
4. Pin the toolbar icon (puzzle-piece menu). Click it to switch the screensaver off while you watch something; a grey `off` badge shows while it is off. Click again to turn it on.

Checks that only the Chromebook can answer are in `README.md` ("Checklist only the Chromebook can answer").

## Data notes (2026-10-30)
History rows written before the rename or the Caddy cleanup were rewritten in place (`spot` source became `alm`; Caddy raw JSON became sentences). A pre-change copy of the database was taken to the session scratch directory, not kept in the repo. `~/.local/share/spot` is a symlink to `alm` and `~/.config/systemd/user/spot.service.bak` is the old unit; both are safe to delete.

## Explanations (what an entry is and why it is there)
```
.venv/bin/python tools/explain.py todo            # kinds of entry in history with no explanation, most frequent first
.venv/bin/python tools/explain.py import knowledge/explanations.json
.venv/bin/python tools/explain.py export knowledge/explanations.json   # then commit it
```
They live in the `explanations` table of `history.db` and are never pruned. A pass is: run `todo`, write JSON records (`sig`, `source`, `category`, `title`, `what`, `why`) for what it lists, import, export, commit. `todo` reads the live database (read-only in effect); `import` writes to it, which is safe while the service runs. Stored text may use `{ip} {n} {user} {path} {who} {status} {cmd}` placeholders.

## Troubleshooting

| Symptom | First look | Likely cause / fix |
|---|---|---|
| Saver never appears | options page: auto-lock line | auto-lock is not later than the saver timeout; the lock screen wins. Lower the timeout or raise auto-lock. Also check the toolbar badge is not `off`. |
| Saver closes unexpectedly | options page **Recent activity** | read the reason: `closed by us: page asked {edge-swipe ...}` (a touch in the 24 px edge zone, coordinates included), `screen locked`, or `closed EXTERNALLY` (OS or user). |
| Saver reopens right after closing | activity log "idle state" lines | the idle detector was re-armed. Fixed in 0.3.0+; do not call `setDetectionInterval` without an interval change. |
| Page shows "alm offline, retrying" | `systemctl --user status alm` | service down or container asleep; the page reconnects by itself (backoff to 15 s). |
| No myserver entries, `alm` warn "lost myserver journal, retrying" | `ssh -o BatchMode=yes myserver true` | key or network problem. The follower retries with backoff up to 60 s. |
| No Caddy entries | `ssh myserver 'sudo -n tail -n 1 /var/log/caddy/access.log'` | needs passwordless sudo, or put the user in the `caddy` group and drop `:sudo` |
| Timeline buttons do nothing | startup flags | `--no-history`, or mock mode (history is off). |
| Timeline empty or sparse | `sqlite3 ~/.local/share/alm/history.db 'select count(*), datetime(min(t),"unixepoch") from entries'` | history too young, or backfill disabled/failed (ssh down at start). |
| Orphan `ssh ... tail` processes | `pgrep -af 'ssh.*tail'` | should not happen (children are reaped); if they do, the service was killed outside systemd. Kill them by hand. |
| Container restarted, nothing running | `systemctl --user status alm` | linger must be `yes` (`loginctl show-user $USER -p Linger`). |

Resetting history: stop the service, delete `history.db*` (all three files), start it. The next start backfills 24 h again. Do not delete it while the service runs.

## Data and privacy notes

- History holds raw log messages (including client IPs, usernames tried by scanners, request URLs) for 7 days on the Chromebook's disk.
- The web port is loopback-only by default. If you ever bind it elsewhere, set a token.
- The ssh follower uses `BatchMode=yes`, so it never prompts; it relies on key auth.
- The extension stores the ALM token (if any) in `chrome.storage.sync`, which syncs with the Google account.

## Conventions for whoever works on this next

- Git: small commits as work lands, plain messages, no attribution trailers.
- Tone: terse, opinionated, no em dashes in prose; verify by running things rather than assuming, and finish a chunk by saying what runs, how, and what is still unverified.
- Outward-facing or persistent actions (installing services, pushing, sharing) need the maintainer's OK.
- Update `DESIGN.md` when a behavior decision changes, `ARCHITECTURE.md` when structure changes, `TODO.md` when work lands or is found.
