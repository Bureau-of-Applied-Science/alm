# ALM architecture map

Read this first. It explains how the system is put together, where each responsibility lives, and which rules must not be broken. `DESIGN.md` is the product spec (why it looks and behaves this way), `OPERATIONS.md` is how to run and debug the live install, `TODO.md` is what is left.

Contents: 1 What it is | 2 Runtime topology | 3 Module map | 4 The entry pipeline | 5 Stream lifecycle | 6 Engine events and client actions | 7 Time model | 8 Presence | 9 History and timeline playback | 10 Ingestion | 11 Burst detector | 12 Web client | 13 Terminal client | 14 Chrome extension | 15 Desk events | 16 Tests | 17 Invariants | 18 Recipes | 19 Hard-won lessons

---

## 1. What it is

ALM is an ambient log monitor. Log entries ("infodrops", "streams") drift right to left across a screen like digital rain. They are meant for peripheral vision: you see that a lot is happening, or that it is quiet, without reading. Any entry can be caught (frozen and opened) to read in full.

It is made by the Bureau of Applied Science. The reference setup runs it on a Chromebook as a screensaver that shows what an Ubuntu server `myserver` is logging (sshd probes, firewall blocks, fail2ban bans, Caddy requests). It also works on any Linux box reading local files or the local journal.

Shape of the code: **one headless engine owns all state; every UI is a thin client.** Python only on the server side, plain HTML/JS in the browser, no build step anywhere.

## 2. Runtime topology

```
 myserver (Ubuntu server, the thing being watched)
   journald  /var/log/fail2ban.log  /var/log/caddy/access.log
        |
        |  ssh -T (BatchMode, key auth): `journalctl -f -o json`, `tail -F`, `sudo -n tail -F`
        v
 Chromebook, Crostini container (Debian 12)   <- systemd user service `alm`
 +----------------------------------------------------------------------------+
 |  main.py                                                                   |
 |    tail.py ---------> RainEngine.ingest() --+--> History (SQLite, 7 days)  |
 |    (followers,        (rain_engine.py)      |                              |
 |     parsers,                |               +--> BurstDetector            |
 |     backfill)               | pub/sub events                               |
 |                             +--> rain_web.py  (FastAPI: /, /ws, /api/state)|
 |                             +--> rain_tui.py  (Textual, only without       |
 |                                                --web-only)                 |
 +-----------------------------|----------------------------------------------+
                               | ws://localhost:8000/ws  (Crostini forwards the port to ChromeOS)
                               v
 ChromeOS Chrome
   web/index.html   normal tab, or  ?screensaver=1  fullscreen window
   extension/       MV3: on chrome.idle opens the fullscreen window + keep-awake
```

Key facts:
- The engine runs where the screen is (Crostini), not on myserver. Nothing is installed on myserver; it is read over ssh.
- The engine is the only process with state. A browser tab, the TUI and the extension never talk to each other, only to the engine (the extension only talks to the page and to Chrome).
- Default bind is `127.0.0.1`. `--token` (or `ALM_TOKEN`) is required on non-loopback binds (a warning is printed otherwise).
- Python changes need a service restart. `web/index.html` is read from disk on every `GET /`, so page changes are live on reload. Extension changes need a reload at `chrome://extensions`.

## 3. Module map

| File | Lines | Responsibility |
|---|---|---|
| `rain_engine.py` | ~550 | `Stream` dataclass and `RainEngine`: the sole state authority. Ingest gating, lifecycle (catch/release/dismiss/exit), presence/away, pub/sub, playback orchestration, desk events, snapshot, plus the mock data generator. |
| `playback.py` | ~220 | Pure timeline logic (no asyncio, takes the clock as an argument): windows, durations, the digest planner (`select_bucket`), `Playback` class, label helpers. |
| `history.py` | ~105 | `History`: SQLite store, buffered writes, unique index, retention. |
| `burst.py` | ~55 | `BurstDetector`: N suspicious events from one public IP in a window become one synthetic alert. |
| `explain.py` | ~100 | Entry signatures (source plus message with addresses, numbers, users and paths replaced by placeholders), `lookup`, `fill`. What an entry is and why it is there. |
| `tail.py` | ~360 | Everything that feeds the engine: `parse_line`, per-source parsers, `tail_file`, `follow_command` (subprocess follower with backoff), ssh/journal helpers, cursor sync. |
| `rain_web.py` | ~105 | `create_app(engine, token)`: FastAPI. `GET /`, `GET /api/state`, `WS /ws`. Translates JSON actions into engine calls, forwards every engine event to the socket. |
| `rain_tui.py` | ~350 | Textual terminal client (`LogRainApp`, `RainView`, `DetailScreen`). |
| `main.py` | ~100 | CLI flags; wires sources, engine, web server and TUI into one asyncio loop. |
| `web/index.html` | ~780 | The whole browser client (CSS + JS in one file): lanes, rendering, catch UI, timeline clock, screensaver behavior, desk events, auto-reconnect. |
| `extension/` | | Chrome MV3 extension: `manifest.json`, `background.js` (service worker), `options.html` + `options.js`. |
| `deploy/` | | systemd user units: `alm-crostini.service` (the live one), `alm.service` (generic). |
| `tools/fake_log.py` | | Writes a slowly evolving fake log for demos (`--tail`). |
| `tools/explain.py` | ~75 | `todo` / `import` / `export` for the explanation catalogue (section 12). |
| `knowledge/explanations.json` | | The committed copy of the explanations stored in `history.db` (193 kinds at 2026-10-30). |
| `tests/` | | pytest suite (see section 16). |
| `DESIGN.md` | | Product spec. |

Dependency direction (no cycles): `main` -> `rain_web`, `rain_tui`, `tail`, `rain_engine`, `history`; `rain_engine` -> `burst`, `history`, `playback`; `playback` -> `history` (for the `Row` type); `tail` -> `rain_engine` (type only, it calls `engine.ingest` / `engine.record`). The engine imports nothing from the UI or ingestion layers.

## 4. The entry pipeline

Every real log line follows one path. The **order of the gates matters** and is deliberate.

```
source line
  -> parser (tail.py)            -> (level, source, message, ts)
  -> engine.ingest():
       1. history.add()          ALWAYS first. Playback needs the evidence even for entries never shown.
       2. burst.observe()        BEFORE sampling, so rate-limiting cannot hide a scan. A hit may call ingest() again with a synthetic "alm" error.
       3. info/debug rate limit  token bucket, INFO_PER_SEC = 2 (burst up to 4). warn/error skip this gate entirely.
       4. banking                if away OR a playback is running: warn/error go to engine.flagged, info is dropped. Return.
       5. level filter           live view filter (all/info/warn/error/debug).
       6. MAX_ACTIVE = 30        safety cap on concurrently alive streams.
       7. _add_stream()          the ONLY place a Stream is born; broadcasts a `spawn` event.
```

Side paths into the same machinery:
- `engine.record()`: history only (no display, no burst). Used by sync (entries older than 60 s on arrival).
- `engine.desk()`: history only, source `desk`. Used for screensaver interaction events (section 15).
- Playback (`_playback_tick`) and away-replay (`replay_flagged`) also go through `_add_stream(..., replay=True)`.
- Mock mode (no real source given): `_spawn_loop` generates fake entries, history is disabled. Real sources turn mock off and history on.

Levels are exactly `debug | info | warn | error`. Parsers map everything else onto these.

## 5. Stream lifecycle

A `Stream` is one displayed entry. The server does **not** track position. It stores `spawn_time` (monotonic) and `duration`, and every renderer computes where the stream is from `elapsed = now - spawn_time`. Speed is therefore identical on every client without any position ticks.

```
 spawn --(elapsed >= duration)--> exit
   |  ^
 catch| |release           catch: freezes elapsed (caught_elapsed), records owner + time
   v  |                   release: shifts spawn_time so elapsed resumes where it froze
 caught --dismiss--> dismissing (fade 0.6s, frozen_elapsed) --> exit
```

- `duration = (12.0 + min(len(message), 110) * 0.03) * (1.15 if replay)` seconds. It is the single speed knob for all renderers: speed = (viewport + footprint) / duration.
- Messages longer than the reading window (`WINDOW_W = 44` chars in the TUI, a CSS-based window in the browser) scroll their text inside the window over the first ~94% (TUI) / 90% (web) of the traversal. That is a renderer detail derived from `elapsed`.
- **One stream caught at a time.** `catch()` releases any other caught stream first.
- **Catches have owners.** `catch(id, owner)`: the websocket handler passes `ws<id>`, the TUI passes `"tui"`. When a socket closes, `release_owner(owner)` lets go, so closing a window cannot strand a frozen drop. `CAUGHT_TTL_S = 120` releases a catch nobody touched.
- `_reap` runs every 0.25s: flushes history, prunes hourly, expires stale catches, and removes finished streams while broadcasting `exit`.
- **Lanes are a purely local rendering concept.** Each client assigns its own lane with its own overlap rule (a lane is reusable once its occupant is 72% through). Clients never agree on lanes and need not.

## 6. Engine events and client actions

### Events (engine -> subscribers; the websocket forwards each as JSON)

| `type` | Fields | Meaning |
|---|---|---|
| `snapshot` | `present, filter, flagged_count, flagged_reason, playback, streams[]` | Sent once on connect. Clients **reset to a clean slate** and rebuild. Streams carry `elapsed`, `caught`, `dismiss_elapsed`. |
| `spawn` | `id, level, source, message, ts, duration, replay` | New stream at elapsed 0. |
| `catch` / `release` | `id, elapsed` | Freeze / resume. |
| `dismiss` | `id, elapsed` | Start the fade. |
| `exit` | `id` | Remove the stream. |
| `presence` | `present` | Away/at-desk toggled. |
| `filter` | `level` | Live filter changed. |
| `flagged_count` | `count, reason?` | Banked warn/error count. `reason` is `away` or `playback`. |
| `playback` | `state` (`start/pause/resume/stop/end/snapshot`), `window_s, v_start, v_end, rate, virtual_now, paused, skipped, flagged_only` | A timeline anchor. Clients derive the clock from it; the server never ticks the clock. |
| `playback_stats` | `skipped, shown` | 1 Hz while a playback runs. |

Subscribe with `engine.subscribe()` (returns an `asyncio.Queue`); always `unsubscribe` in a `finally`.

### Client actions (browser -> `/ws`, JSON `{action, ...}`)

`catch{id}`, `release{id}`, `dismiss{id}`, `toggle_presence`, `set_filter{level}`, `playback{window: "5m"|"30m"|"1h"|"6h"|"12h"|"24h"}`, `playback_pause`, `playback_resume`, `playback_stop`, `desk{kind: opened|input|dismissed, detail}`.

The engine validates (`set_filter` rejects unknown levels, `desk` rejects unknown kinds and empty detail). Never trust the page.

`GET /api/state` returns the same snapshot (token-checked). `WS /ws` closes with code `4401` on a bad token; the token is a `?token=` query parameter, compared with `hmac.compare_digest`.

## 7. Time model

Two clocks, never mixed:
- **Monotonic** (`time.monotonic()`): stream positions, lanes, rate limiting, burst windows, playback pacing. Immune to wall-clock jumps.
- **Wall epoch** (`time.time()` floats): history rows, playback windows, display labels. Sources that supply a real timestamp (journal `__REALTIME_TIMESTAMP`, Caddy `ts`) pass it through; otherwise arrival time is used. `fmt_ts` renders `HH:MM:SS`, with a weekday prefix for entries from another day during playback.

Tests substitute a fake clock only for the `time` name inside `rain_engine` (section 16).

## 8. Presence

`present` (at desk) vs away. While away, only warn/error are banked in `engine.flagged` and the live rain stops spawning. On return (`toggle_presence`):
- With history available: "warn/error since you left" is played back through the timeline machinery (`start_playback(window, flagged_only=True)`, window clamped to 60 s - 24 h).
- Without history (mock): the old `replay_flagged()` paced replay.

Presence is toggled manually (a header button or `a` in the TUI). Detecting it from an SSH attach is an open idea (TODO).

## 9. History and timeline playback

### History (`history.py`)
- SQLite table `entries(t REAL, level, source, message)` with a unique index on `(t, source, message)` (so backfill overlapping live ingest does not duplicate). WAL mode.
- Writes are buffered in memory and flushed by the engine's 0.25s reap loop and before every read. Messages are capped at 4000 chars.
- Retention 7 days (`--keep-days`), pruned hourly, and capped at 5M rows (`--max-rows`, oldest dropped first). Default file `~/.local/share/alm/history.db` (`--db`). `--no-history` disables history and with it the timeline.
- Only real sources are stored, never mock data. Tests use `--no-history` by default so they cannot pollute the real DB.

### Sync (replaces the old start-only backfill)
The engine is a replica of what the watched host already keeps (journald, Caddy and fail2ban files), not a live tap. Each follower stores a **cursor** in the `cursors` table (same flush/transaction as the entries, so a cursor never runs ahead of its data) and resumes from it on every start and every reconnect, so laptop sleep, a service restart or an ssh drop leaves no hole.
- Journal: `journalctl -f -o json --after-cursor=<__CURSOR>`; with no cursor yet, `--since "<--backfill> hours ago"` (default 24, 0 = from now). A cursor the journal rejects (vacuumed) is dropped and the next start begins from the window.
- Files (Caddy, fail2ban): `tail -F -n 20000`, records at or before the stored epoch are dropped. Needs a timestamp per line: Caddy has one; fail2ban's server-local stamp is converted with the host's `date +%z` (`Fail2ban` parser). `plain` files have none and follow from now (`-n 0`). Rotated logs (`.1`, `.gz`) are not read.
- Arrival older than `LIVE_WINDOW_S` (60 s) means sync: `engine.record` (history only, no display, no burst detector, the known gap). Fresh entries are shown as before.
- One shared ssh connection per host (`ControlMaster=auto`, socket in `$XDG_RUNTIME_DIR`): servers commonly rate-limit new ssh logins (ufw LIMIT on 22, `MaxStartups`), and a four-login reconnect storm was observed being refused.

### Playback (`playback.py`, driven by the engine)
A virtual clock sweeps `[now - window, now]` over a fixed real duration: 5m in 30 s, 30m in 60 s, 1h in 90 s, 6h in 150 s, 12h in 180 s, 24h in 240 s (other windows, as in away-return, interpolate; minimum 15 s). Entries appear when the virtual clock reaches their timestamp, so the little clock in the UI is the time the entries "happened".

Playback is a **digest**, not a transcript. At construction the whole plan is computed up front, one bucket per second of playback, per-bucket cap about 2 (`BUDGET_PER_S`):
1. Errors are never dropped.
2. Remaining room: half to *priority* warns (non-noisy), half to the general pool (info and noisy warns); either side takes what the other cannot fill.
3. A message *signature* (`source + first two words`, digits masked) that repeats at least 20 times and more than 2% of the window is **noisy** (scanner probes, cron, firewall blocks). Noisy warns are demoted to the general pool; non-noisy entries go first there, except one bucket in four where noisy ones lead so noise stays represented.
4. Every pool is shared round-robin across sources, rotated per bucket, each source contributing an evenly spread sample.

The `skipped` count (`+37` on the clock) is entries the clock passed that were not shown.

During playback: live info is hidden; live warn/error are banked and the UI shows "N live since". On stop or end they replay like an away-return. `pause_playback` / `resume_playback` / `stop_playback` are engine methods; the engine runs `_playback_loop` at 0.25 s (plus a 1 Hz stats event).

## 10. Ingestion (`tail.py`)

| Source | Flag | Mechanism |
|---|---|---|
| Local file | `--tail FILE` (repeatable) | polling tail (`tail_file`), JSON or `[HH:MM:SS] LEVEL source message` lines, anything else becomes info |
| Local journal | `--journal` | `journalctl -f -o json --after-cursor=...` via `follow_command` |
| Remote journal + fail2ban | `--ssh HOST` | `ssh HOST journalctl -f ...` and `ssh HOST tail -F /var/log/fail2ban.log` (both resumable) |
| Remote file | `--ssh-file HOST:PATH:PARSER[:sudo]` | `ssh HOST [sudo -n] tail -F PATH`; parsers `caddy`, `fail2ban`, `plain`; paths containing `:` unsupported |

`follow_command` runs a long-lived subprocess, parses each stdout line, and restarts it with exponential backoff (2 s doubling to 60 s; reset after 30 s healthy). The first loss per outage is announced as a `warn` from source `alm` ("lost X, retrying"). Optional `key`/`cursor_of`/`resume`/`prepare` make it resumable (see Sync). **The child is always reaped in `finally`** (a leak of orphaned `ssh ... tail` processes was a real bug); the systemd unit also uses `KillMode=control-group`.

Parser behavior worth knowing:
- Journal: `PRIORITY` 0-3 error, 4 warn, 5-6 info, 7 debug; source is `SYSLOG_IDENTIFIER` (or `_COMM`). Kernel lines containing `[UFW ...]` become source `ufw`, text `block TCP 1.2.3.4 -> :5432`, level info. `sshd` info lines matching probing patterns (Failed, Invalid user, authentication failure, bad negotiation, ...) are raised to **warn**.
- fail2ban: `Found` warn, `Ban`/`Restore Ban` error, `Unban` info, source `f2b:<jail>`. Its own timestamp is dropped (server-local time) so arrival time is used.
- Caddy access JSON: 5xx error, 4xx warn, else info, source `caddy`, message is a sentence: `<who> <verb> <path> [(status)] from <ip>`, e.g. `Safari on iPhone viewed / from 1.2.3.4`, `GPTBot loaded /fonts/a.woff2 from ...`, `curl probed /.env (404) from ...`. Who comes from the User-Agent (bot name, tool, or browser on OS); verbs: viewed, loaded (assets), probed (scanner paths, dotfiles), requested (other 4xx), was redirected at, sent POST to. Assets (`loaded`) a visitor fetches within 10 s of viewing a page are folded into that view by `CaddyFold` (stateful, one per followed file): they go to history only (`HistoryOnly` records, `engine.record`), never to the rain. Host is dropped (one site per log today), paths cap at 60 chars.
- Caddy's own service log (certificates, ACME, reverse proxy) arrives via the journal as JSON; `tail.caddy_service` turns it into `logger: msg · subject` (the subject, a host or request, comes after ` · ` and is left out of the explanation signature). Lines of 100+ characters are cut at 110 plus 80 for the subject. History rows written before 2026-10-30 were rewritten in place to this form.

### Collector (`alm_collector.py`, runs on the watched host)
The durable record lives on the host, so a screen can be asleep, off or new and still sync everything. Stdlib only (Python 3.10), deployed with `tail.py` and `burst.py` beside it (`deploy/install-collector.sh HOST`, a user systemd unit; nothing needs root).
- `run`: follows the host's journal and log files with the same parsers and `follow_command` (a `Sink` stands in for the engine), stores every entry in SQLite with a sequence number (`entries(seq, t, level, source, message, quiet)`), keeps its own follower cursors, prunes (14 days, 3M rows), and raises **burst alerts** itself as ordinary `alm` error entries, so they exist while nobody watches. `quiet` marks Caddy assets folded into a page view. SIGTERM cancels the followers so their children are reaped.
- `stream --after <storeid>:<seq> --since EPOCH`: what a client runs over ssh. One JSON line per entry (`c` cursor, `t`, `l`, `o`, `m`, `q`), then follows, with `{"hb": ...}` heartbeats every 15 s. A cursor from a different store id (db reset) restarts from `--since`.
- Client: `main.py --collector HOST` (replaces `--ssh`/`--ssh-file` for that host). `tail_collector` is one resumable `follow_command` (cursor key `collector:HOST`) over the shared ssh connection; the engine sets `local_bursts = False` so alerts are not raised twice. Old entries arrive history-only, fresh ones are shown, folded ones are `HistoryOnly`.
- Failure model: collector down means the client still shows `lost HOST collector, retrying` and resumes by cursor when it returns; the client down means nothing is lost.

## 11. Burst detector (`burst.py`)

Counts per public IP: every `ufw` entry at any level, plus warn/error from `sshd`, `caddy`, `f2b:*`. Private, loopback and multicast addresses are ignored (documentation ranges such as 203.0.113.x are also non-global, so tests use real public IPs). Eight hits in 60 s produce one synthetic **error** from source `alm`: `1.2.3.4 hit us 12x in 60s (3 ports, sshd+ufw)`. Cooldown 300 s, re-alerting earlier only if the pace quadruples. It runs inside `ingest` before sampling. Known issue: an sshd probe and the matching fail2ban "found" count twice.

## 12. Web client (`web/index.html`)

One file, no framework. Structure: CSS, markup (header, `#lanes`, timeline controls and clock, footer, `#detailPanel`, `#ssOffline`), then one IIFE.

- **Rendering**: each stream is a DOM element animated with the Web Animations API (a linear `translate` over the remaining duration, plus an inner `translateX` to reveal long text). Catch pauses both animations; release plays them; `snapshot` restores `elapsed`. Log text is untrusted and always goes through `esc()` before `innerHTML`.
- **Lanes**: `floor(height / 44)` lanes (min 4), free-lane choice is random among free ones (local only).
- **Websocket**: auto-reconnect with backoff 0.5 s -> 15 s. Every `snapshot` calls `resetStreams()` first (no duplicates or strands after a reconnect). In screensaver mode a "alm offline, retrying" notice shows while disconnected.
- **Catch**: click sends `catch`; the detail panel opens only when the `catch` event comes back for the id it asked for (`pendingCatchId`), so a snapshot never pops a panel. Click outside or Escape releases.
- **Nameplate** (`#nameplate`, first child of `#app`): the Bureau of Applied Science mark (inline SVG, ~29 KB, copied from bureauofapplied.science `logos/logo-2-line.svg`) plus an `ALM-1 AMBIENT LOG MONITOR` legend, centered at `z-index:0` below `#lanes`, `pointer-events:none`, low opacity. Inline because the server serves only `index.html`. It is part of the screensaver too.
- **Atmosphere**: scanlines and a vignette sit on `#app::before`, *beneath* the lanes, vignette capped at 25%, so only the lane edge fade dims text (readability fix).
- **Catch-up**: `resume_live` calls `_start_catchup`: if history exists, no playback is running and the pause lasted at least `CATCHUP_MIN_S` (5 s), it builds a `Playback` from `_paused_wall` to now, sets `pb.kind = "catchup"`, clears `flagged` (they are in the replay) and starts it through `_begin_playback` (shared with `start_playback`). The `playback` event and snapshot carry `kind` (`timeline` or `catchup`). Otherwise `replay_flagged` as before. In the page, `kind === "catchup"` changes the clock legend to `CATCH-UP`, shows `LIVE IN m:ss` (remaining virtual time over the rate) in `#tlMeta`, and ends as `LIVE` instead of `ENDED`.
- **Clock glitch**: when the `tail` expires, `glitchOut()` adds `.glitch` to `#tlClock` (CSS `clockGlitch`, 0.52 s, `steps(1)`), then clears `tail` and hides it after 540 ms; a new playback cancels it (`glitchTimer`); `prefers-reduced-motion` skips it.
- **Live pause**: `pause_live` / `resume_live` (ws `live_pause`, `live_resume`; event `live_pause{paused}`; snapshot `live_paused`). It uses the same stream hold as a timeline pause. `ingest` banks warn/error as it does while away, with `flagged_reason` `paused`; resume starts a catch-up replay (above) or, for a short pause, replays the banked warn/error. Refused while away or playing; `start_playback` and `toggle_presence` clear it without replaying. The page has one `togglePause()` (the Pause key and Space) choosing between playback and live pause.
- **Pause holds the streams**: `pause_playback` sets `held_elapsed` on every stream not caught or dismissing (`_hold_streams`), so `elapsed()` stops and `_reap` cannot expire them; `resume_playback` and `stop_playback` shift `spawn_time` back (`_unhold_streams`); streams spawned or released while held start held. The page eases playback rates to 0 over ~0.8 s (`syncHold`) and back over ~0.5 s; the TUI just stops. The clock outlives the playback (`tail`): it stays, frozen, until no `.stream.replay` is left (1.5 s minimum, so the banked replays that follow a stop can spawn).
- **Timeline**: the lower-right `#tlLabel` toggle (`setTlOpen`, closes after 6 s; timer not `:hover`, which sticks after touch) opens `#tlControls`, whose window chips send `playback{window}`. The clock module (`#tlClock`: plate legend, HH:MM, 60-segment stepped strip `#tlRing` driven by `stroke-dashoffset`, `-6H`, `+N`, RUN/HOLD/AWAY lamps) is derived locally from the last `playback` anchor (`virtual_now`, `rate`, `paused`) with `performance.now()`; the server only sends anchors. Badge "N live since" returns to live.
- **Screensaver mode** (`?screensaver=1`, optional `&ext=<extension id>&token=...`): header and cursor hidden after 3 s of calm; streams stay interactive (full-lane-height hit area, hover highlight); the timeline toggle sits at the lower right (inboard of the exit zones) and opens on tap. **Exit** = inward swipe from any edge (24 px zone, 64 px travel, Pointer Events) or Escape. `leave()` sends the desk `dismissed` event, then after 150 ms tells the extension to close the window, with `window.close()` only as a delayed fallback (closing first raced the extension's message).
- Query parameters: `screensaver`, `token`, `ext`, `deskquiet` (milliseconds, test override for the 30 s quiet gap).

## 13. Terminal client (`rain_tui.py`)

Textual app. Computes cell positions locally from `elapsed`, same as the browser. Keys: `a` away/present, `1-4` filter (all/info/warn/error), `t` cycle timeline (live, 5m ... 24h, live), `p` pause, `x` back to live, `q` quit. Clicking a stream catches it (owner `"tui"`) and opens `DetailScreen`. The status bar shows the playback clock (via `playback.clock_label`) and "N live since". It is only started when `--web-only` is not given; the live service runs web-only.

## 14. Chrome extension (`extension/`, MV3, v0.4.1)

Purpose: ChromeOS has no API to replace its built-in screensaver, so the extension imitates one.

- **Permissions**: `idle`, `power`, `storage`. `externally_connectable` for `http://127.0.0.1/*` and `http://localhost/*` so the ALM page can message it.
- **Flow**: `chrome.idle.onStateChanged` -> `idle` -> `show()`: open a fullscreen window at `<url>?screensaver=1&ext=<id>[&token=]` and `chrome.power.requestKeepAwake('display')`. It closes on the page's `dismiss` message or on `locked`. **It never closes on `active`** (the user is interacting with the saver; closing on `active` was the original bug). `hide()` also releases keep-awake.
- **State**: `chrome.storage.session` holds `windowId` and `closingId` (MV3 service workers die and restart freely, so nothing important lives in memory). `closingId` lets `windows.onRemoved` tell our close from an external one.
- **Settings** (`chrome.storage.sync`): `url` (default `http://127.0.0.1:8000`), `token`, `timeoutSec` (default 300, minimum 15), `enabled` (default true).
- **On/off**: the toolbar button toggles `enabled`, with a grey `off` badge and tooltip when disabled; turning it off closes a saver that is up. The options page checkbox applies immediately. Use: switch it off while watching something.
- **Idle re-arm rule**: `setDetectionInterval` is only called when the interval actually changed (`armedInterval`), because re-arming can immediately report `idle` and reopen a saver just closed. `storage.onChanged` reacts to the `sync` area only, never to our own log writes.
- **Activity log**: `chrome.storage.local.events` (last 40) records idle states, opens, closes and their reasons (`closed by us: page asked {edge-swipe...}`, `screen locked`, `closed EXTERNALLY`), page breadcrumbs, desk events, on/off. Shown on the options page under "Recent activity". This is the first thing to read when the saver misbehaves.
- **Messages**: page -> extension (`runtime.sendMessage(extId, ...)`): `dismiss{why}`, `note{what, detail}`; both are accepted only from the saver window. Extension pages -> background: `show`, `hide`, `toggle`, `test-idle{state}` (the last doubles as the test hook; `active` is deliberately a no-op there).
- **Auto-lock**: if the screen locks before the idle timeout, `idle` never fires and the saver never shows; the options page shows `chrome.idle.getAutoLockDelay()` with a warning.

## 15. Desk events

While the saver is up the page reports `opened` (first websocket open), `input` (first input after 30 s of quiet; the first 2 s after load are ignored because a new window fires synthetic pointer events), and `dismissed` (with how). The engine stores them with `desk()`: source `desk`, levels info/warn/warn. They are **history only**: they appear in timeline playback and in the extension's activity log, never as live drops, never banked, broadcast or counted by the burst detector. They show that something touched the machine and when, not who, and nothing is recorded while the device sleeps.

## 16. Tests

`.venv/bin/pytest` runs the whole suite (170 tests at last run, roughly ten minutes; the extension and browser ones are the slow part). Browsers: Playwright Chromium (`playwright install chromium`).

| File | Covers |
|---|---|
| `test_engine.py` | lifecycle with the fake clock, ingest gating, sampling, banking, catch ownership and TTL, desk |
| `test_playback.py` | digest planner, fairness, noisy demotion, clock/labels, engine playback (fake clock), away-return |
| `test_history.py`, `test_burst.py` | store semantics (including the row cap); burst thresholds |
| `test_explain.py` | entry signatures, placeholder fill, lookup, the shipped catalogue is complete |
| `test_tail.py` | all parsers (including Caddy access sentences and Caddy's own service log), followers, cursor sync, child reaping |
| `test_collector.py` | collector store/stream/burst, client parse, an end-to-end collector plus client over a real file |
| `test_tui.py` | Textual pilot |
| `test_browser.py` | `Server` fixture (real uvicorn on a free port, `--no-history` by default, optional `db`); caught-on-connect, reconnect, token |
| `test_ui.py`, `test_timeline_ui.py` | Playwright against the real page: screensaver exit swipes (including CDP touch), interactivity, timeline UI, desk events end to end |
| `test_extension.py` | the unpacked extension loaded into real Chromium, driven through its message hook |

Patterns and traps:
- `tests/conftest.py` `clock` fixture is a `Clock` whose `.t` is monotonic time and `.wall()` the fake epoch. It is installed as a **shim for `rain_engine.time` only**. Patching the global `time.monotonic` freezes asyncio.
- Real `chrome.idle` transitions and ChromeOS keep-awake cannot be tested here; they go through `test-idle`. Only the Chromebook can answer the README checklist.
- Headless Playwright keeps stale `Page` objects for closed windows; extension tests ask Chrome (`chrome.windows.getAll`) via `window_count()`.
- Clicking a moving stream times out; pause animations mid-lane first (`pause_mid_lane`).
- The MV3 service worker sometimes starts slowly; `ext_id()` waits up to 30 s and nudges.
- Never let a test write to the real history DB (the `Server` fixture defaults to `--no-history`).

## 17. Invariants (do not erode)

1. The engine is the only owner of state. Clients send actions and render events; they never decide lifecycle.
2. Positions are derived from `elapsed`, never ticked from the server. Events are discrete (spawn, catch, ...), not positional.
3. Every stream is born in `_add_stream`. Every client action goes through the same engine method regardless of caller.
4. History is written before any gating or sampling. The burst detector runs before the rate limit.
5. `warn` and `error` are never rate-limited and never dropped from banking; errors are never dropped from the playback digest.
6. Log text is untrusted: escape in the DOM, validate in the engine.
7. Desk events never reach the live view.
8. The extension never closes the saver on `active`, and never re-arms the idle detector without an interval change.
9. A child process started by the engine must be reaped on cancel.
10. Tests never touch the real history DB or the global monotonic clock.

## 18. Recipes

- **Add a log source**: write a `parse_*(line) -> (level, source, message, ts|None) | None` in `tail.py`; register it in `FILE_PARSERS` if it is file-shaped, or call `follow_command(engine, argv, parse, label)` from a new `tail_*`; add the CLI flag in `main.py`. Add parser tests with sample lines.
- **Explanations** (section 12 detail view): `engine.explain(stream)` computes the entry's signature and reads the `explanations` table in `history.db` (never pruned; same file, separate table). The page sends `explain{id}` after a catch and the server answers that socket only with `{type:"explain", found, category, title, what, why}`; the TUI calls `engine.explain` directly. Stored text uses `{ip}`, `{n}`, `{user}`, `{path}`, `{cmd}`... which `fill` replaces from the entry. `found:false` shows "Not catalogued yet". The pass that writes them is `tools/explain.py todo|import|export`, with the copy kept in git at `knowledge/explanations.json`. **Seam for the API-key future:** `explain.lookup` is the one place that sees a miss; generate there and store with `put_explanation(..., by="api")`.
- **Add a websocket action**: handle it in `rain_web.py`'s dispatch, implement it as an engine method (validate input), broadcast an event if other clients need to know, handle the event in `web/index.html` `onMessage` and in `rain_tui.py` if relevant, test both ends.
- **Add an engine event**: broadcast from the engine only; document it in section 6; make `snapshot()` carry any state a late joiner needs.
- **Tune the digest**: constants in `playback.py` (`BUDGET_PER_S`, `NOISY_MIN`, `NOISY_SHARE`, `NOISY_SLOT_EVERY`, `DURATIONS`). Check against real data by playing 24h through a scratch `History` copy, not by reasoning.
- **Change speed or feel**: `ENTRY_BASE_DURATION_S` and `ENTRY_LENGTH_COEF_S` in `rain_engine.py`; the rest of DESIGN section 8 lists the calibrated parameters.
- **Ship an extension change**: bump `version` in `manifest.json`, run `tests/test_extension.py`, commit, then reload the extension at `chrome://extensions` (Chrome does not do it for you; the options page title shows the version).
- **Ship an engine change to the live service**: run the tests, commit, `systemctl --user restart alm`, confirm `curl -s -o /dev/null -w '%{http_code}' http://localhost:8000/` returns 200.

## 19. Hard-won lessons

- A frozen "static infodrop" at saver start came from a catch whose owner (a closed window) never released it. Hence catch ownership and the TTL.
- Touch dismissal: the first extension closed on `active`. Fixed by explicit dismissal and by logging why; touches no longer dismiss (user confirmed on the device).
- Closing the window from the page raced the extension's dismiss message; the page now asks the extension first.
- Writing to the activity log re-armed the idle detector, which reopened the saver. Hence sync-only `onChanged` and `armedInterval`.
- sshd probes were about 100% of the 24 h digest until noisy-template demotion and per-source fairness (now sshd about 46%, mostly never-dropped errors).
- A trailing comment on a systemd `Key=value` line is invalid; comments go on their own line.
- `main.py` once shadowed `host` with a loop variable (now `remote`).
- Documentation IP ranges are non-global, so the burst detector ignores them.
