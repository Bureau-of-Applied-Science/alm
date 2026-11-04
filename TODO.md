# ALM todo

Last updated 2026-10-30 (end of the house-style / explanations session). How the code fits together: `ARCHITECTURE.md`; live install and debugging: `OPERATIONS.md`. Specs for the interface batch are in `DESIGN.md` (4.6, 5.5, 5.6, and the 8/9 additions). Order matters: each step lists the test that proves it.

## Interface batch (built 2026-10-29; D2 demo and Chromebook confirmation remain)

### A. Readability
- [x] **A1. Vignette beneath text.** Move vignette and scanlines to a layer below the Streams, cap corner darkness at ~25%, leave the edge fade as the only thing that dims text (DESIGN 4.6). Also: the message right-edge fade mask applies only when the message overflows its window (short messages like `-> :5432` were losing their last characters).
  Test: Playwright reads computed contrast/opacity of a mid-lane stream (>= ~90%), screenshot compared by eye.

### B. Interactive screensaver
- [x] **B1. Streams stay interactive in `?screensaver=1`.** Remove "any input closes", remove `pointer-events:none`, show the detail panel, full-lane-height hit area, hover highlight (DESIGN 5.5).
  Test: in screensaver mode a click catches, outside click releases, mouse movement and keys (except Escape) do not close.
- [x] **B2. Inward edge swipe exits.** Pointer Events, 24 px edge zone, 64 px inward threshold, all four edges; Escape also exits.
  Test: swipe from each of 4 edges exits; from the centre, along an edge, or short of threshold does not.
- [x] **B3. Extension stops closing on `active`.** Page sends an explicit dismiss to the extension (`externally_connectable` for the ALM origin); extension closes on that message or on `locked`, never on `active`. Keep `window.close()` as fallback.
  Test: extension test drives idle -> active and asserts the window survives, then dismiss closes it and releases keep-awake.

### C. Timeline exploration
- [x] **C1. History store.** SQLite, epoch timestamps, written before sampling, pruned to retention (default 7 days, `--keep-days`).
  Test: write/read/prune with a fake clock; survives engine restart.
- [x] **C2. Backfill for journal sources** (`journalctl --since`), local and ssh, 24h by default.
- [x] **C3. Engine playback.** Windows 5m/30m/1h/6h/12h/24h, virtual clock, per-minute entry timing, display budget with warn/error priority, `+N` overflow count, pause/stop, live entries banked during playback, `playback` event and snapshot field, away-replay reimplemented as "since I left" (DESIGN 5.6, 7.4).
  Test: fake-clock suite: an entry stamped 14:07:30 spawns while the clock reads 14:07; error never dropped, warn before info; budget respected; stop returns to live; banked live entries replay after.
- [x] **C4. Web timeline UI.** Window buttons, stop/pause, little clock (analog + HH:MM + `-6h` + progress ring + day tick), "N live since" badge.
  Test: Playwright: start 5m, clock advances from `now-5m` to `now`, Replay badge on streams, stop returns to live.
- [x] **C5. Timeline in screensaver mode** (faint bottom handle, decision 1).
- [x] **C6. TUI timeline.** Key to pick a window, clock in the status bar.

### D. Close out
- [x] **D1. README and DESIGN sync** after each step lands (README currently describes the older screensaver behavior).
- [x] **D2. Demo** against live server data (24h backfill, 24h playback, screenshots checked).

## Next: house style batch (opened 2026-10-30)
- [x] **E0. Nameplate** behind the datafeed, history row cap.
- [x] **E1. Clock restyle** (2026-10-30). Playback module with plate, recessed display, stepped 60-segment strip and RUN/HOLD/AWAY lamps (DESIGN 4.7, 5.6). Protocol unchanged; timeline tests pass, with the meta assertions made case-insensitive because the readout is now uppercase.
- [x] **E1b. Window controls** (2026-10-30). One faint lower-right `TIMELINE` toggle (right-thumb reach, opposite the clock) opens a column of keys; folds away after a choice or 6 s. Clock dimmed and moved behind the streams. Chips keep the old unobtrusive pill look; transport-key styling was dropped in favor of that. Not yet judged on the Chromebook display (thumb reach, brightness).

- [x] **E3. Inspect view** (2026-10-30). Release and Dismiss buttons removed; detail shows an explanation (DESIGN 5.8). 162 kinds explained in a first Claude pass over the live history. Green muted again.
- [x] **E5. Clock lingers; pause eases** (2026-10-30). The clock stays until the replays leave the screen; Pause holds streams in the engine and eases them to a stop in the page.
- [x] **E6. Pause the live view** (2026-10-30). Same hold and easing as a timeline pause; warn/error bank and replay on resume (DESIGN 5.9).
- [x] **E7. Catch-up and clock glitch** (2026-10-30). Resuming a live pause (5 s or more) replays what arrived as a catch-up with a countdown to live (DESIGN 5.9); the clock leaves with a glitch (5.6).
- [ ] **E2. Chassis furniture** for the rest of the chrome (header, filters, status) now that the clock and timeline keys set the grammar. Keep it quieter than the data (DESIGN 4.7); the look of the lanes stays.
- [ ] **E8. Judge the new look on the Chromebook display.** Only headless screenshots so far: clock dimness and position, the lower-right toggle's thumb reach, the pause easing (0.8 s), the catch-up countdown, the glitch, green and amber legibility at saver distance.
- [ ] **E9. Shorten the long `sudo` (up to ~250 chars) and `tailscaled` (up to ~240) lines** the way Caddy's were. Not asked for yet; about 2% of entries.
- [ ] **E10. A brighter "live is held" cue** while a timeline plays or live is paused: the dimmed clock and faint toggle are easy to miss (a 24h playback once looked like "no new streams"). The status line and toggle already change when live is paused.
- [ ] **E4. Keep the catalogue fresh.** Run `tools/explain.py todo` now and then (new kinds appear as new things happen). Later: API-key fallback at `explain.lookup` for misses, and explanations that look at the specific path or user for Caddy probes.

## Decisions (2026-10-29)
1. Timeline controls: a faint toggle at the lower right (inboard of the exit-swipe zones, reachable with the right thumb) that expands to the window keys on tap; the same in normal and screensaver mode. (Originally a bottom-centre handle; changed 2026-10-30.)
2. Playback pacing: the DESIGN 8 table stands (5m plays in 30s ... 24h in 4 min, mostly a digest).
3. Live while a timeline plays: live entries hidden, warn/error banked, badge reads "N live since"; on stop or end they replay like an away-return.
4. Backfill: on by default, 24h, for journal sources (local and ssh). `--backfill HOURS` to change, `0` to disable.
5. Retention: 7 days (`--keep-days`).

## Found in the live demo
- [x] **Digest fairness.** Fixed: noisy-template demotion plus per-source fairness (DESIGN 5.6). On the real 24h, sshd fell from ~100% to 46% of the digest (111 of it the never-dropped errors), with ufw, CRON, Caddy, tailscale and sudo now visible. Remaining: Caddy's own service log shows as raw JSON lines (parse its `msg`), CRON is still over-represented on this server.
- [ ] Backfill does not run the burst detector, so the scan bursts that a live run would flag are absent from history.

## Done: desk events
- [x] Screensaver reports opened / input after quiet / dismissed to history only (playback + extension log, never live). DESIGN 5.7.

## Done: extension on/off
- [x] Toolbar button toggles the screensaver (grey `off` badge, closes a saver that is up); options checkbox applies at once. Extension v0.4.0. Idea not built: timed snooze ("off for 1h", re-enables itself).

## Carry-over (not part of this batch)
- [ ] **Chromebook verification** (README checklist): real idle trigger, whether keep-awake prevents auto-lock, fullscreen over the shelf, container up while idle, extension loads from "Linux files".
- [ ] Caddy over ssh uses `sudo -n tail`. Prefer `usermod -aG caddy ubuntu` on myserver and dropping `:sudo`.
- [ ] Burst detector counts an sshd probe and its fail2ban "found" as two hits; consider deduping.
- [ ] Pin severity to lanes vs random lane; lanes vs depth at full-monitor scale.
- [ ] Presence from SSH attach/detach; swappable `is_flagged(entry)` interface.
- [ ] Fluent Forward / OTLP / `/ingest` HTTP endpoint: not started; ssh tailing covers current needs.

## Done
- Systemd user service `alm` installed and enabled on the Chromebook (`./install.sh --service`); linger on.
- Digest fairness, stranded-catch fix (catch ownership + TTL), extension activity log, desk events, extension on/off.
- Engine, TUI, web, shared-state events (reference build).
- Caught-on-connect fix (snapshot freezes caught and dismissing streams); one-caught-at-a-time in the engine; HTML escaping of log text.
- File and journald ingestion, `--tail`, `--journal`, ssh sources (`--ssh`, `--ssh-file`) with reconnect; sshd probe, ufw, fail2ban, caddy parsers; info rate limit; burst detector.
- Token auth on `/ws` and `/api/state`; loopback default bind.
- Auto-reconnecting web client; screensaver mode v1; Chromebook extension v1 (idle -> fullscreen window + keep-awake).
- Interface batch A1, B1-B3, C1-C6 (history store, backfill, playback engine, web and TUI timeline): vignette beneath text, interactive screensaver, inward edge-swipe exit, extension no longer closes on `active`.
