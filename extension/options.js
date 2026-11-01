const DEFAULTS = { url: 'http://127.0.0.1:8000', token: '', timeoutSec: 300, enabled: true };
const $ = (id) => document.getElementById(id);

function say(text, warn = false) {
  $('status').textContent = text;
  $('status').className = warn ? 'warn' : '';
}

async function load() {
  const s = { ...DEFAULTS, ...(await chrome.storage.sync.get(DEFAULTS)) };
  $('enabled').checked = s.enabled;
  $('url').value = s.url;
  $('token').value = s.token;
  $('timeoutSec').value = s.timeoutSec;
  await showAutoLock(s.timeoutSec);
}

// ChromeOS only. If the screen locks before our idle timeout, the idle state goes straight to "locked"
// and the saver never shows, so surface the mismatch.
async function showAutoLock(timeoutSec) {
  const el = $('lock');
  if (typeof chrome.idle.getAutoLockDelay !== 'function') { el.textContent = ''; return; }
  const delay = await chrome.idle.getAutoLockDelay();
  if (!delay) { el.textContent = 'Screen auto-lock: never.'; el.className = ''; }
  else if (delay <= timeoutSec) {
    el.textContent = `Screen auto-locks after ${delay}s, which is not later than the saver timeout. The saver will never appear; lower the timeout or raise auto-lock.`;
    el.className = 'warn';
  } else { el.textContent = `Screen auto-locks after ${delay}s.`; el.className = ''; }
}

$('save').onclick = async () => {
  let url = $('url').value.trim();
  try { new URL(url); } catch { return say('That is not a valid URL.', true); }
  const timeoutSec = Math.max(15, parseInt($('timeoutSec').value, 10) || DEFAULTS.timeoutSec);
  await chrome.storage.sync.set({ url, token: $('token').value, timeoutSec, enabled: $('enabled').checked });
  $('timeoutSec').value = timeoutSec;
  await showAutoLock(timeoutSec);
  say('Saved.');
};
$('enabled').onchange = async () => {      // an on/off switch should not need a Save
  await chrome.storage.sync.set({ enabled: $('enabled').checked });
  say($('enabled').checked ? 'Screensaver on.' : 'Screensaver off. Nothing will open until you turn it back on.');
};
chrome.storage.onChanged.addListener((c, area) => { if (area === 'sync' && c.enabled) $('enabled').checked = c.enabled.newValue; });
$('test').onclick = async () => { await chrome.runtime.sendMessage({ type: 'show' }); say('Opened. Move the mouse to close it.'); };
$('stop').onclick = async () => { await chrome.runtime.sendMessage({ type: 'hide' }); say('Stopped.'); };
async function showLog() {
  const { events = [] } = await chrome.storage.local.get('events');
  $('log').textContent = events.length ? events.slice().reverse().map((e) =>
    new Date(e.t).toLocaleTimeString() + '  ' + e.what + (e.detail ? '  ' + e.detail : '')).join('\n') : '(nothing yet)';
}
$('refresh').onclick = showLog;
$('clear').onclick = async () => { await chrome.storage.local.set({ events: [] }); showLog(); };
chrome.storage.onChanged.addListener((_c, area) => { if (area === 'local') showLog(); });
showLog();
$('ver').textContent = 'v' + chrome.runtime.getManifest().version;
load();
