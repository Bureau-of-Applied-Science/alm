# ALM — Ambient Log Monitor
### Master design context for abstract recreation

This document specifies the *design* — the concept, visual language, interaction
rules, and architectural pattern — independent of any particular language,
framework, or codebase. Someone should be able to rebuild this faithfully from
this document alone, in whatever stack fits their situation. A reference
implementation exists (Python/Textual for terminal, FastAPI/WebSocket for web)
but it is one instantiation of this spec, not the spec itself.

---

## 1. Purpose & vision

ALM is an ambient, glanceable log monitor for a desk-bound developer/operator.
It is explicitly **not** a log viewer or a dashboard — it's peripheral-vision
software. The goal is to make "a lot is happening" or "it's quiet" legible
without reading anything, while still allowing any individual entry to be
caught and read in full on demand.

Long-term trajectory (not yet built, but the design should not foreclose it):
a local model watches the same underlying log/event sources, flags entries
that matter, and — while the user is away from the workstation — banks those
flagged entries instead of surfacing them live. On return, the banked entries
replay as a distinguishable catch-up sequence. The current implementation
mocks the "which entries matter" judgment with a simple severity heuristic
(warn/error) so the interaction shape can be validated before any real model
work happens.

End-state form factor: a dedicated monitor, always on, ambient. Accessible
both as a terminal application (so it can run headless, over SSH, close to
the actual log sources) and as a browser view (so it can sit on a second
monitor or be checked from anywhere) — both showing and controlling the
**same live state**, not two independent simulations that happen to look
alike.

---

## 2. Core metaphor: digital rain, reinterpreted

The reference is the Matrix "digital rain" effect, but the rules worth
stealing are not about the color green — they're structural:

1. **A cascade has a bright leading edge and a fading tail.** The head is
   "now"; the tail is "a moment ago," dissolving toward nothing.
2. **The cascade's length is fixed, not the content's.** A column doesn't get
   taller because there's more data — it cycles at a constant footprint. This
   is the direct answer to "what happens with a long log line": don't stretch
   the visual to fit the content; compress the content into a fixed visual
   budget and reveal it progressively.
3. **Density communicates state before anything is read.** A busy screen vs.
   a quiet one should be legible peripherally, at a glance, from across a
   room — this is the entire point of the ambient framing.

The orientation is rotated 90° from the literal reference: motion flows
**right to left** (horizontal), not top to bottom. This was a deliberate
choice, not an arbitrary translation — text still reads left-to-right while
exiting left, so nothing on screen ever reads backwards or upside down, which
a literal vertical translation would risk.

---

## 3. Terminology / glossary

| Term | Meaning |
|---|---|
| **Stream** | One log entry's live, animated representation. The unit of everything below. |
| **Lane** | A horizontal track a Stream travels along. Purely a rendering concept — see §8.3. |
| **Head** | The leading edge of a Stream (rightmost-arriving, or the tag/level marker). |
| **Reveal window** | The fixed-width budget a message's text is shown through; see §5.3. |
| **Catch** | User action: freeze a Stream in place and open its full detail. |
| **Release** | Resume a caught Stream from exactly where it was frozen. |
| **Dismiss** | Freeze a Stream in place, then fade it out and remove it — distinct from Release. |
| **Presence** | Whether the user is "at desk" (ambient mode) or "away" (banking mode). |
| **Flagged / banked** | An entry judged worth surfacing, held back while the user is away. |
| **Replay** | The visually-distinguished playback of banked entries on return. |
| **Fresh** | A brief highlighted state on a just-spawned Stream (see §7). |
| **Engine** | The single authoritative process/module owning all Stream state (see §8). |

---

## 4. Visual & motion language

### 4.1 Layout: lanes

The viewport is divided into horizontal lanes stacked to fill available
height. Each lane can host one Stream, or — under the overlap rule in §7 —
briefly two. More lanes = more concurrency visible at once; lane count is a
function of available space, not a fixed number, so a terminal with 30 rows
and a browser window with 15 apparent rows both feel proportionate to their
own real estate.

### 4.2 Motion: full traversal, constant speed

A Stream always travels the **entire** width of the viewport — fully
off-screen on the entering edge to fully off-screen on the exiting edge —
regardless of message length or duration. Speed is derived once from
`(viewport width + Stream footprint) / duration` and held constant for the
whole journey; it is linear, not eased, which reads as more mechanical/data-
like than a bouncy UI motion would.

**Design pitfall to avoid, stated explicitly because it was hit and fixed
during the reference build:** if your rendering system expresses position as
a *static offset* (e.g., "anchored at 100% of the container") *plus* an
*animated transform* on top of it, the two can silently compound and the
Stream will stop short of the far edge — appearing to "finish" mid-screen
rather than exit. Derive the full travel path from a **single** coordinate
model (one position variable driven by one continuous function of elapsed
time), not layered/stacked coordinate systems that both contribute
displacement on the same axis.

### 4.3 Truncation & the reveal window

Every message is shown through a fixed-width "reveal window" — a character
or pixel budget that does not grow with message length. If a message is
shorter than the window, it just sits there statically. If longer, the
window's contents scroll (marquee) continuously over roughly the Stream's
full lifetime, so the *rate* of reading through a long message is
proportional to its length, but the *footprint on screen* never is. This
motion should be continuous and linear — never finish early and freeze,
which reads as broken/cut-off rather than intentional.

This is the direct implementation of metaphor rule #2 in §2.

### 4.4 Fade: edges and dismissal

Two independent fade mechanisms:

- **Edge fade (ambient, position-based):** near both the entering and exiting
  edges of a lane, opacity ramps smoothly to zero, purely as a function of
  on-screen position — not a timer. This means it behaves correctly even if a
  Stream is paused mid-fade (it just stays exactly as faded as its position
  implies), and it never produces a hard, sudden cutoff. A Stream must remain
  interactive (catchable) at any opacity above ~0 — fading is not the same as
  becoming inert.
- **Dismiss fade (explicit, time-based):** triggered only by the Dismiss
  action (§6.3). The Stream freezes in place first, *then* fades to zero
  opacity over a short fixed duration (order of half a second), then is
  removed. This is deliberately different from edge fade — it's a distinct,
  intentional "this is going away now" signal, not ambient dissolution.

### 4.5 Color language: three separate channels

Do not let one visual property carry more than one meaning. Three independent
signals, each with its own color/treatment:

1. **Severity** (info/warn/error/debug, or your real taxonomy) — carried
   mainly by the head/tag and a tint on the message text itself. This is a
   property of the *content*.
2. **Interaction state** (caught/selected, cream since 2026-10-30) — a distinct accent color, not
   reused from severity, so "you are looking at this" never gets confused
   with "this is an error." A ring/glow/border treatment reads well.
3. **Freshness** (just arrived) — a brief brightness/glow pulse independent
   of both of the above, most valuable during lane overlap (§7) where it
   distinguishes the new arrival from the older, fading occupant beneath it.

Replay entries (§6.4) get their own fourth treatment (a distinct accent plus
an explicit tag/badge) so a replayed-from-history entry is never mistaken for
something happening live right now.

### 4.6 Atmosphere: scanlines and vignette

*Status: built 2026-10-29.*

The vignette and scanlines are part of the look and stay. The rule is that
**atmosphere never sits on top of text.** Only the edge fade (§4.4) may dim a
Stream, and it does so as a function of position alone.

- Render the vignette and scanlines on a layer *beneath* the Streams, not as
  an overlay blended over them. Today they are an overlay on `#app::after`,
  which darkens message text by up to ~55% near the edges, stacking on top of
  the edge fade, and that is what makes lines hard to read.
- Cap the vignette darkness well below the old value (target <= ~25% at the
  corners) so the background still reads as vignetted without competing with
  the edge fade.
- Acceptance check: a Stream at mid-lane renders its message at >= ~90% of
  full contrast; a Stream in the exit-fade zone is dimmed by the edge fade
  and nothing else.

### 4.7 House style: Bureau of Applied Science, cassette futurism

*Status: nameplate built 2026-10-30; the rest is direction for the clock and timeline restyle.*

ALM is a Bureau of Applied Science tool and reads as one piece of hardware.
The page is a digital display *inside* a physical chassis panel, so scanlines
stay (they belong to the display) while panel furniture is physical.

- **Name**: ALM, model ALM-1, "Ambient Log Monitor". Short form for the
  command and service (`alm`); the long form on plates and legends.
- **Nameplate** (built): Bureau mark and `ALM-1 AMBIENT LOG MONITOR` legend,
  behind the datafeed (ARCHITECTURE 12). It never takes input and never sits
  above text (same rule as 4.6).
- **Bureau palette** (from the site): amber `#C9740E`, burnt orange `#BA581C`,
  cream `#F9EDD9`; site ground `#FBF8F3`, raised `#F1ECE3`, rule `#97897A`,
  text `#1A1714`. Amber and burnt orange are the accent family for chassis
  furniture (plates, legends, control families). The level colors in 4.5
  stay as they are; chassis color must not be confused with level color.
- **Datastream palette** (revised 2026-10-30, from mockups in
  `temp/`, a general look and not law): a CRT glass of warm green-black
  (`#0B100D`) with phosphor-green data and amber for attention.
  info `#43A251` tag on `#58B264` text (muted phosphor green: pastel read as too pale, full saturation as too bright, 2026-10-30); warn amber `#F2A73B`; error
  hot orange-red `#EA5B34` (the only red-ish, kept apart from amber by hue,
  the stronger band and the head tag); debug dim green-gray `#6F8A76`.
  Warn and error lines sit on a soft phosphor band like a highlighted CRT
  line; info and debug do not. Text keeps the horizontal ghost smear plus a
  faint bloom. Channels stay separate (4.5): **caught** is cream
  `#F4E6C4`, **replay** is amber-gold `#E8B84A` (amber is preferred
  over teal; it sits close to warn, so the `REPLAY` badge and the head tag
  do the disambiguating; light gray is the fallback if that bites), and the
  clock module uses chassis amber `#E0951F`, not a data color. The TUI
  mirrors these values.
- **Type**: site uses Libre Franklin / Inter and system mono. Physical labels:
  tracked uppercase grotesk. Display content: the existing mono.
  Identifiers carry model codes and numbering (Bureau habit: `01`, `FIGURE 01`).
- **Tone of copy**: plain, direct, understated; uppercase short legends.
- **Cassette futurism rules to apply** (the brief is a source of ideas, not
  doctrine): physical hierarchy (chassis, subassemblies, controls), controls
  that each have a job and a legend, state shown by lamps and counters,
  discrete stepped motion (START, STOP, SEEK, HOLD) over eased motion,
  no glassmorphism. Built: the timeline clock (5.6) as a panel instrument.
- **Unobtrusive first.** The chassis furniture must stay quieter than the
  data. The clock sits *behind* the streams (z-index 0, no pointer events,
  translucent, dim lit parts), so a passing entry is never covered. Controls
  are faint until touched. Add chassis detail only where it does not compete
  with a stream for attention.

---

## 5. Interaction model

### 5.1 Catch

Clicking/selecting a Stream:
- Freezes it exactly where it is — position, reveal-window progress, and any
  in-progress fade all stop advancing.
- Suppresses the ambient edge-fade treatment while caught (it should read at
  full clarity regardless of where on screen it was caught).
- Opens a detail view: full untruncated message, timestamp, source, severity,
  and an **explanation**: a category, a one-line title, *what* the entry is and
  *why* it is there (5.8). No Release or Dismiss buttons; you leave by tapping
  outside, Escape, or the ×. (Changed 2026-10-30.)
- Catching a new Stream while one is already caught should release the
  previous one automatically — only one thing caught at a time.

### 5.2 Release

- Resumes motion and reveal-window progress from precisely where it left off.
  This requires **elapsed-time-preserving** pause semantics: record how much
  of the Stream's lifetime had elapsed at catch time, and on release, treat
  "now minus that elapsed amount" as the new effective start time — do not
  let paused time count against the Stream, and do not restart it from
  scratch.
- Triggered by: an explicit Release action, clicking anywhere outside both
  the detail view and the caught Stream itself, or a dedicated "back out"
  key (e.g. Escape).

### 5.8 Explanations (what is this and why is it here)

*Status: built 2026-10-30.*

The detail view answers the question a caught entry raises. Entries are
grouped by **signature**: source plus message with addresses, numbers, user
names and paths replaced by placeholders, so every `block TCP x -> :n` is one
kind of thing. Each kind gets one stored explanation (category, title, what,
why) in the history database, reused for every later entry of that kind, with
the entry's own values filled in (`{ip}`, `{n}`). Writing them is a **pass**
done by Claude over the signatures that `tools/explain.py todo` lists; the
file `knowledge/explanations.json` is the committed copy. Entries nobody has
explained yet show "Not catalogued yet". Later, an API key can fill misses
automatically at the lookup seam (`explain.lookup`) and mark them `by=api`.
Explanations describe the kind of event in general terms; they must not claim
facts about myserver that the log does not show.

### 5.9 Pausing the live view

*Status: built 2026-10-30.*

**Pause** (the first key under the lower-right timeline toggle, or Space; `p` in
the TUI) holds the live screen the way pausing a timeline does: the streams on
screen ease to a stop and wait. While held, new entries are still recorded in
history; warn and error entries bank ("N held back, click to resume") and the
rest are not shown. **Resume** eases the streams back up and, if the pause
lasted 5 s or more, starts a **catch-up**: a short timeline from the moment you
paused to now, replaying what arrived (same digest and pacing as 5.6, tagged
`REPLAY`). The clock shows it with the legend `CATCH-UP` and a countdown to
live (`LIVE IN 0:12`) next to the sweeping time and the segment strip, and the
banked warn/error entries are in the replay, not shown twice. A shorter pause
just replays the banked warn/error entries. Entries that arrive during the
catch-up bank and replay when it ends, as after any timeline. One key serves both modes: during a timeline
it pauses the replay, otherwise it pauses live. Starting a timeline or going
away clears a live pause (what banked stays banked). Held state is engine state
(ARCHITECTURE 12), so every client shows it.

### 5.3 Dismiss

- Distinct from Release. Freezes the Stream in place (same elapsed-preserving
  freeze), then runs the dismiss fade (§4.4) and removes it. This is the
  "I've seen this, make it go away" action, as opposed to Release's "let it
  keep drifting."

### 5.4 Presence (away/at-desk) & replay

- Two states: **at desk** (ambient mode — everything spawns and drifts
  normally) and **away** (banking mode — ambient spawning stops being shown;
  only entries judged worth surfacing, per whatever flagging logic is in
  use, are held in a queue instead).
- Returning to at-desk mode plays the banked queue back as a paced sequence
  of Replay-tagged Streams (§4.5), then resumes normal ambient behavior.
  Replay entries should feel *slightly* different in pace/treatment from
  live ones — not just tagged, but perceptibly a catch-up reel.
- In the current build, presence is a manual toggle. The intended real
  signal, once this is terminal/SSH-hosted, is session attachment: no active
  session attached = away; a session (re)connecting = at-desk and triggers
  replay. This is a cleaner proxy than any desk-presence heuristic and falls
  naturally out of hosting the app over SSH — worth preserving as the target
  design even though it isn't implemented yet.

### 5.5 Screensaver mode (browser, full screen, unattended)

*Status: built 2026-10-29.*

The screensaver is the browser surface (`?screensaver=1`) opened full screen
by an idle trigger (on ChromeOS, the extension in `extension/`). It is
**interactive**: you can catch, read, release and dismiss Streams as they pass
(§5.1-5.3) exactly as in the normal browser surface. Moving the mouse, typing,
or touching a Stream does **not** end it.

- **Leaving the screensaver:** an inward swipe that starts at any of the four
  screen edges. Detected with Pointer Events so touch, pen and mouse-drag all
  work: `pointerdown` within the edge zone, then travel inward past the
  threshold (§8) before release. Escape also leaves it, for keyboard-only use.
  A drag that starts inside the screen or travels along the edge does nothing.
- **A catch in progress is not interrupted** by the idle signal returning.
  Because the user is now active, the OS idle state flips to `active`; the
  extension must not treat that as "close". It closes the window only on the
  page's explicit dismiss message (sent on edge swipe or Escape) or when the
  screen locks.
- **Hit targets:** Streams are small and move; the clickable area should be the
  full lane height, not just the glyphs. Hovering a Stream highlights it
  (brightness only, no motion change); clicking catches it.
- **Releasing the display:** closing the window releases the keep-awake request.
- **Switching it off while you watch something:** the extension's toolbar button
  toggles `enabled` (grey `off` badge; a saver already up closes), and the
  options checkbox does the same and applies at once. While off, no idle event
  opens anything. A timed snooze is not built.
- **Invoking the timeline (§5.6) with no chrome on screen:** a faint
  `TIMELINE` toggle at the lower right (inboard of the exit-swipe zones, in
  right-thumb reach, opposite the clock) that expands into a column of window
  keys when tapped, and folds away after a choice or a few idle seconds. Same
  control in normal and screensaver mode. Pause and Live sit in the column while
  playing. (Revised 2026-10-30; it was a bottom-centre handle opening on approach.)

### 5.6 Timeline exploration (chronological playback)

*Status: built 2026-10-29 (engine, browser, screensaver handle, TUI).*

Away-replay (§5.4) answers "what did I miss since I left". Timeline
exploration generalizes it to "show me the last N, in order": the same
catch-up reel, but over a chosen window of history.

**Windows:** 5 min, 30 min, 1 h, 6 h, 12 h, 24 h, always ending now.

**Playback model.** A *virtual clock* sweeps from `now - window` to `now`
over a fixed playback duration (§8), not at wall-clock speed. Each stored
entry enters the screen when the virtual clock reaches the minute of its
original timestamp (an entry stamped 14:07:30 enters while the clock reads
14:07, positioned within that minute by its seconds). After entering, a Stream
lives and traverses at normal real-time speed (§4.2); only *entry times* are
compressed. All replay Streams carry the Replay treatment (§4.5).

**The clock.** *Restyled 2026-10-30 (4.7).* A playback module on the chassis
shows the virtual time at the playback head: a plate legend (`PLAYBACK`,
`TL-1`), a recessed display with an `HH:MM` readout in the viewer's local time
(replay color, glow), the window label (e.g. `-6H`, `+N` skipped), and a
60-segment strip that fills in discrete steps with a scale (`-6H` to `NOW`).
Three lamps show state: `RUN`, `HOLD` (blinks, with the digits, while paused)
and `AWAY` (the since-you-left replay). The display flickers for a few frames
when it engages. There are no eased animations. When a window crosses midnight
the readout adds a day prefix (`TUE 09:12`). It appears only during playback.
The clock **outlives the playback**: when it ends (or you tap Live) it stays,
frozen where it stopped, with `ENDED` and the `END` lamp, for as long as any
replayed stream is still crossing the screen, including the banked live entries
replayed on return. **Pause** holds every stream on screen: the engine freezes
their clocks, and the page eases them to a stop over about 0.8 s (and back up
over 0.5 s on resume) instead of freezing them dead. (2026-10-30.)
When the clock is ready to go (replays gone), it leaves with a **glitch**
instead of just vanishing: about half a second of stepped slices, sideways
jumps, an amber/cream colour split on the digits and brightness flashes, ending
in a collapse to a line. Reduced-motion users get a plain hide. It is cancelled
if a new playback starts.
The analog dial and smooth ring were dropped: a stepped counter fits the
hardware grammar better.

**Density.** Compression can put far more entries into a second than the
screen can hold (a 24 h window may contain thousands of entries in a few
minutes of playback). Playback is therefore a *digest*, not a transcript: a
per-second display budget is planned up front, one bucket per second of
playback, so the digest is fair across the whole window and not just within a
tick. Per bucket: errors are never dropped; the remaining room is split half to
*priority warns* and half to the general pool (either side takes what the other
cannot fill); every pool is shared round-robin across sources, with the start
rotated per bucket so no source is always first. A message template that
repeats heavily across the window is *noisy* (a template is the source plus the
first two words with digits masked, and noisy means more than 2% of the window
and at least 20 repeats). Noisy warns lose the priority slot, and in the general
pool noisy entries go after calm ones except in one bucket of four, so a
representative sample of the noise stays visible. A rare warn, such as an
accepted login, always keeps priority.
(Measured on a real server: 24 h held ~10,400 entries, ~2,650 of them warns,
which is ~11 warns/s at 24 h playback speed against a screen that holds ~2
new Streams/s, so warns cannot all be shown either.) The clock label shows how
many entries a minute held beyond what was shown (`+37`). Full-fidelity
reading is explicitly out of scope; that is a log viewer, which ALM is not.

**Live data while playing.** Live ingest continues and is stored. While a
timeline plays, live entries are not shown; warn/error are banked exactly as
when away (§5.4) and the badge reads "N live since" (decided). On exit or completion ALM
returns to live and, if anything was banked, offers/plays it as an ordinary
away-replay.

**Controls.** Start a window, stop (back to live), pause/resume. Browser: window
the lower-right timeline toggle (4.7, 5.5). TUI: `t`
cycles live, 5m, 30m, 1h, 6h, 12h, 24h, live; `p` pauses; `x` returns to live. Catch,
release and dismiss work on replay Streams as on live ones; pausing the
timeline also holds the virtual clock.

**Relationship to away-replay.** Away-replay becomes the special case "window =
since I left". One mechanism, two entry points.

**Source of truth.** History needs a persistent store (see §9). The engine
owns playback state (§7.1); renderers receive an anchor (`virtual_start`,
`real_start`, `rate`) in a `playback` event and compute the clock locally,
consistent with §7.4. Spawn events during playback carry the original
timestamp so the detail view shows when the entry *happened*.

### 5.7 Desk events (who touched the machine)

*Status: built 2026-10-29.*

While the screensaver is up, interaction with it is itself worth knowing about
later. The screensaver page reports three kinds of event to the engine over the
existing WebSocket (`{action: "desk", kind, detail}`):

- `opened`: the saver came up (info).
- `input`: the first input after a quiet gap (default 30 s, `?deskquiet=` for
  tests), with how long it had been quiet and the input type, e.g.
  `input after 2.1h quiet (touch)` (warn). A freshly opened window's own
  synthetic pointer events (first 2 s) are ignored.
- `dismissed`: how it was left, e.g. `dismissed by edge swipe from right
  (touch)` or `dismissed by Escape` (warn).

**Where they appear, and where they do not.** They are recorded to history only
(source `desk`), so they show up in timeline playback (§5.6), where a rare
template keeps priority in the digest. They are never shown as live drops,
never reach the TUI, and do not trigger burst detection. The same events are
also written, in readable form, to the extension's activity log on its options
page. This is deliberate: a touch is something to read about after the fact, not
something to flash on a screen you may be looking at.

**Limits.** ALM can say that something touched the saver and when, not who.
The page only sees input while it is open; a sleeping device records nothing.

---

## 6. Concurrency: lane overlap & highlighted refresh

Lanes are **not** a strict one-at-a-time queue. A lane becomes eligible for a
new arrival once its current occupant has progressed through roughly
**70–75% of its lifetime** — i.e., once it's substantially through being
read/displayed and is in (or near) its exit fade. The new arrival is drawn on
top (in front, in stacking order) of the still-finishing original and gets a
brief "fresh" highlight (§4.5) so the handoff reads as an intentional new
arrival cutting across the old one's tail, not a glitch. This keeps
throughput high without waiting for empty slots, and it's a deliberate,
literal expression of "new data arriving over old" rather than something to
avoid.

---

## 7. System architecture pattern

This pattern is what let the terminal and web surfaces genuinely share state
rather than run two parallel simulations that happen to look similar. Anyone
rebuilding this — in any stack — should preserve this separation.

### 7.1 Engine as sole authority

One module/process owns:
- The list of live Streams and their full state (content, timestamps,
  spawn/duration timing, caught/dismissed status, presence mode, filter,
  banked queue).
- All *mutating* logic: spawning new entries, catching, releasing,
  dismissing, toggling presence, replaying the banked queue.
- A publish/subscribe mechanism: any number of listeners (a terminal
  renderer, any number of browser tabs) can subscribe to a live feed of
  every state change, from *any* origin.

No renderer ever mutates Stream state directly. Every renderer, regardless of
medium, calls the same small set of engine actions (`catch`, `release`,
`dismiss`, `togglePresence`, `setFilter`) — this symmetry is what guarantees
a catch performed in one surface is immediately visible and correctly
frozen in every other surface, because there's only one Stream object, not
one per renderer.

### 7.2 Thin, symmetric renderers

Each renderer's job is exactly two things:
1. Turn current engine state into its medium's rendering primitives
   (character cells with color for a terminal; DOM/CSS motion for a browser;
   whatever else for whatever else).
2. Turn user input (clicks, keys, taps) into calls on the engine's action
   methods.

A renderer should contain **no** log-lifecycle logic of its own — no local
"is this expired yet," no local severity banking. If a renderer starts
accumulating that kind of logic, it's drifting from thin-client status and
will eventually desync from the other surfaces.

### 7.3 What's shared vs. what's local

**Shared (engine-owned):** content, timestamps, spawn time, duration,
severity, caught/dismissed status, presence, filter, banked queue.

**Local (per-renderer only):** lane/row assignment. Different surfaces have
different, incompatible geometries (terminal rows vs. browser viewport
height), so forcing a single shared "lane number" across mismatched spaces is
fragile and pointless. Instead, each renderer independently runs the *same
algorithm* (pick a free lane, honoring the overlap-eligibility rule in §6)
against its own geometry, the first time it becomes aware of a given Stream.
Two renderers may legitimately put the same Stream in different visual rows
— that's fine and expected.

### 7.4 Event model: discrete lifecycle events, not position ticks

The engine broadcasts discrete events — *spawned*, *caught*, *released*,
*dismissed*, *exited*, *presence changed*, *filter changed* — each carrying
enough information (duration, elapsed-at-event-time, content) for a receiving
renderer to compute the Stream's position/animation state locally and
continuously from then on. The engine does **not** broadcast a position on
every tick. This keeps the event stream cheap regardless of how many Streams
are active, and keeps each renderer's motion smooth and locally owned rather
than jittering with network/update timing.

*Addition (timeline, §5.6):* a `playback` event carries
`{window_s, virtual_start, real_start, rate, paused, state}`; the `snapshot`
includes the current playback state so a client that connects mid-playback
shows the clock at the right place. The clock is derived locally from that
anchor, never ticked from the server. A 1 Hz `playback_stats` event carries
the skipped-entry counter; it is a counter, not a position.

### 7.5 Pause/resume semantics, precisely

Because catch/release must preserve elapsed progress (§5.2), and because
different renderers may not share a clock, the cleanest implementation is:
track *elapsed time*, not *absolute position*, as the thing that freezes.
When catching, record the elapsed duration at that instant. When releasing,
reconstruct an effective start time as "now minus that recorded elapsed
duration." Every renderer computing position from `(elapsed, duration)`
rather than from a server-pushed coordinate stays correct through pause and
resume without any special-casing.

---

## 8. Calibrated parameters (starting points, not gospel)

These are the values the reference build converged on — reasonable starting
points for a rebuild, worth re-tuning by eye rather than treating as exact:

| Parameter | Value | Notes |
|---|---|---|
| Lane overlap threshold | ~72% of lifetime | When a lane becomes eligible for a new arrival |
| Dismiss fade duration | ~0.6s | Freeze-then-fade window |
| Fresh-highlight duration | ~0.4–0.7s | Brightness pulse on spawn/overlap |
| Edge fade zone | ~12–17% of lane width, each side | Ambient entrance/exit dissolve |
| Reveal window width | fixed character/pixel budget, tuned to taste | Should *not* scale with message length |
| Reveal completion | ~90–95% of lifetime | Marquee should nearly finish, not freeze early |
| Entry lifetime | scales mildly with message length, capped | Longer messages get a bit more time, not unboundedly |
| Replay pacing | ~15% slower than live | Perceptible as "catching up," not live |
| Timeline windows | 5m, 30m, 1h, 6h, 12h, 24h | Always end at now |
| Timeline playback duration | 30s, 60s, 90s, 150s, 180s, 240s for the windows above | Starting guess; tune by eye. 24h plays ~360x |
| Timeline display budget | ~1.5 Streams/sec | Lane count and lifetime cap what the screen can hold |
| Screensaver edge zone | 24 px from each screen edge | Where an exit swipe may start |
| Screensaver swipe threshold | 64 px inward | Travel needed to count as a swipe |
| Vignette darkness | <= ~25% at corners, beneath text | See §4.6 |
| History retention | 7 days | `--keep-days` |
| Journal backfill | 24 h on start | `--backfill HOURS`, 0 disables |

---

## 9. Deferred work / open questions

- **Real flagging logic.** Currently: a hardcoded severity check
  (warn/error banked while away). The eventual local-model-based "does this
  matter" judgment should be swappable in without changing anything about
  the presence/replay interaction contract above.
- **Real presence detection.** Currently a manual toggle. Target: derive it
  from session attachment (see §5.4) once hosted over SSH; would need a
  clean hook for "a session just attached/detached" wherever this is
  eventually deployed.
- **Real log ingestion.** Currently mock/generated data. Wiring in an actual
  source (system journal, `tail -f`, structured log stream, etc.) is a
  content-layer concern and shouldn't require changes to anything in §5–§8.
- **Caught-on-connect edge case.** If a new client (e.g., a browser tab)
  connects while some other surface currently has a Stream caught, that
  client should render it as already-caught/frozen from the start, not miss
  it. Worth an explicit test case in any rebuild.
- **History store (§5.6), built.** SQLite in the data directory, one row per entry
  with an epoch timestamp, written before display sampling so playback has the
  evidence, pruned to a retention (default 7 days, configurable). This also
  lets the away bank survive restarts.
- **Backfill.** A freshly started engine has no history, so a 24 h window is
  empty until it has run for 24 h. Journal sources backfill 24 h on start with
  `journalctl --since` (on by default; duplicates against stored history are
  ignored). File sources do not backfill yet.
- **Timeline scrubbing and speed control** are not specced. Backfill does not
  run the burst detector, so scans from before a restart show as individual
  warns/blocks, not as a single burst alert.
- **Mouse/input portability caveats.** Terminal mouse reporting is a
  standard protocol but can be silently swallowed by terminal multiplexers
  (tmux/screen) unless explicitly enabled — worth a note in onboarding docs
  for whichever rebuild inherits this.

---

## 10. Platform notes (context from exploration, not requirements)

- The built-in ChromeOS screensaver slot was set aside (it offers only Google
  Photos / art, and no hook for third-party content was found), and an OS
  screensaver assumes the page can be frozen while idle, which fights a tool
  that must keep ingesting. What was built instead (2026-10) is a Chrome
  extension (`extension/`) that opens ALM full screen on `chrome.idle` and
  holds the display awake with `chrome.power`. Ingestion does not depend on
  the screensaver: the engine runs independently of the browser window.
- The terminal surface is intended to be a plain program accessed *over*
  SSH, not an SSH-aware program itself — it should not embed or manage any
  SSH server logic; it just needs to render and accept input correctly in a
  standard remote terminal session.

---

## 11. Reference implementation (context only)

A working instantiation of this spec exists: Python + Textual for the
terminal renderer, FastAPI + WebSocket for the web renderer and transport,
both as thin clients over a shared headless engine object, run together from
one process/event loop. This is provided as a working proof that the pattern
in §7 holds up in practice — including a resolved instance of the exact
pitfall described in §4.2 — not as a constraint on how a rebuild should be
done. Any stack capable of (a) owning shared mutable state with a pub/sub
layer and (b) rendering to at least one of "a character grid" or "a DOM" can
implement this spec faithfully.
