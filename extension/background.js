// ALM screensaver: idle -> fullscreen ALM window + keep-awake. The window is interactive, so the OS
// going back to `active` must NOT close it; it closes on the page's explicit dismiss (edge swipe / Escape)
// or when the screen locks.
// State lives in chrome.storage.session because MV3 service workers are killed and restarted freely.

const DEFAULTS = { url: 'http://127.0.0.1:8000', token: '', timeoutSec: 300, enabled: true };
const MIN_TIMEOUT_S = 15;

async function settings() {
  return { ...DEFAULTS, ...(await chrome.storage.sync.get(DEFAULTS)) };
}

function saverUrl(s) {
  const u = new URL(s.url);
  u.searchParams.set('screensaver', '1');
  u.searchParams.set('ext', chrome.runtime.id);   // lets the page message us to dismiss
  if (s.token) u.searchParams.set('token', s.token);
  return u.toString();
}

// Why did the saver open or close? Kept so a surprise dismissal can be diagnosed from the options page.
async function log(what, detail = '') {
  const { events = [] } = await chrome.storage.local.get('events');
  events.push({ t: Date.now(), what, detail: String(detail).slice(0, 300) });
  await chrome.storage.local.set({ events: events.slice(-40) });
}

async function currentWindowId() {
  const { windowId } = await chrome.storage.session.get('windowId');
  if (windowId == null) return null;
  try { await chrome.windows.get(windowId); return windowId; }
  catch { await chrome.storage.session.remove('windowId'); return null; }
}

async function show(reason = 'idle') {
  const s = await settings();
  if (!s.enabled || (await currentWindowId()) != null) return;
  const win = await chrome.windows.create({ url: saverUrl(s), state: 'fullscreen', focused: true });
  await chrome.storage.session.set({ windowId: win.id });
  chrome.power.requestKeepAwake('display');
  await log('opened', reason);
}

async function hide(reason = 'unspecified') {
  const id = await currentWindowId();
  chrome.power.releaseKeepAwake();
  if (id == null) return;
  await chrome.storage.session.set({ closingId: id });      // so onRemoved can tell our close from someone else's
  await chrome.storage.session.remove('windowId');
  await log('closed by us', reason);
  try { await chrome.windows.remove(id); } catch { /* already gone */ }
}

// Re-arming the idle detector can immediately report the current state (including `idle`, which would
// reopen a saver we just closed), so only do it when the interval actually changed.
let armedInterval = null;
async function applySettings() {
  const s = await settings();
  const interval = Math.max(MIN_TIMEOUT_S, Math.floor(s.timeoutSec));
  if (interval !== armedInterval) {
    armedInterval = interval;
    chrome.idle.setDetectionInterval(interval);
  }
  await showBadge(s.enabled);
  if (!s.enabled) await hide('turned off');
}

// The toolbar button doubles as the on/off switch, so "off" has to be visible at a glance.
async function showBadge(enabled) {
  await chrome.action.setBadgeText({ text: enabled ? '' : 'off' });
  await chrome.action.setBadgeBackgroundColor({ color: '#6b7680' });
  await chrome.action.setTitle({ title: enabled ? 'ALM Screensaver: on (click to turn off)' : 'ALM Screensaver: OFF (click to turn on)' });
}

async function toggle(why) {
  const s = await settings();
  await chrome.storage.sync.set({ enabled: !s.enabled });     // onChanged runs applySettings
  await log(s.enabled ? 'turned off' : 'turned on', why);
}
chrome.action.onClicked.addListener(() => toggle('toolbar button'));

chrome.idle.onStateChanged.addListener((state) => {
  log('idle state', state);
  if (state === 'idle') show('idle');
  else if (state === 'locked') hide('screen locked');
  // 'active': the user is interacting with the saver; leave it up
});

// The ALM page (an http://127.0.0.1 or localhost origin, see externally_connectable) asks to be dismissed.
// Only honour it from our own saver window.
chrome.runtime.onMessageExternal.addListener((msg, sender, reply) => {
  (async () => {
    const id = await currentWindowId();
    const ours = id != null && (!sender.tab || sender.tab.windowId === id);
    if (msg && msg.type === 'dismiss') {
      if (ours) await hide('page asked: ' + JSON.stringify(msg.why || {}));
      else if (id == null) await log('dismiss after close', 'the window was already gone');
      else await log('ignored dismiss', `saver window ${id}, sender tab window ${sender.tab ? sender.tab.windowId : 'none'}`);
    } else if (msg && msg.type === 'note' && ours) {
      await log('page: ' + String(msg.what).slice(0, 40), JSON.stringify(msg.detail || {}));
    }
    reply({ ok: true });
  })();
  return true;
});

// The page closes itself on real input (see ?screensaver=1); keep-awake must not outlive the window.
chrome.windows.onRemoved.addListener(async (id) => {
  const { windowId, closingId } = await chrome.storage.session.get(['windowId', 'closingId']);
  if (closingId === id) { await chrome.storage.session.remove('closingId'); return; }
  if (windowId === id) {                 // not closed by us: the OS, the user, or the page itself
    await chrome.storage.session.remove('windowId');
    chrome.power.releaseKeepAwake();
    await log('closed EXTERNALLY', 'the window went away without us asking (OS gesture, window.close, or a crash)');
  }
});

// Only real settings changes (sync area). Our own log and session-state writes must not re-run this.
chrome.storage.onChanged.addListener((_changes, area) => { if (area === 'sync') applySettings(); });
chrome.runtime.onInstalled.addListener(applySettings);
chrome.runtime.onStartup.addListener(applySettings);
applySettings();

// Only extension pages can message us (no externally_connectable), so this doubles as the test hook.
chrome.runtime.onMessage.addListener((msg, _sender, reply) => {
  (async () => {
    if (msg.type === 'show') await show('options page: Test now');
    else if (msg.type === 'hide') await hide('options page: Stop');
    else if (msg.type === 'toggle') await toggle('test hook');
    else if (msg.type === 'test-idle') {
      if (msg.state === 'idle') await show('test hook');
      else if (msg.state === 'locked') await hide('test hook: locked');      // 'active' is deliberately a no-op
    }
    reply({ ok: true, windowId: await currentWindowId() });
  })();
  return true;
});
