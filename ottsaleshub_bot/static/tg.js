/* Shared bootstrap for both Mini Apps: Telegram handshake, API client,
   back-button stack, toasts, formatting. */
export const tg = window.Telegram?.WebApp;
const params = new URLSearchParams(location.search);
const devToken = params.get('token') || localStorage.getItem('panelToken') || '';
if (params.get('token')) localStorage.setItem('panelToken', params.get('token'));

/* Telegram's SDK throws WebAppMethodUnsupported for several calls when the page
   isn't actually running inside a Telegram client. An uncaught throw here would
   abort the module before any screen renders, so every call is isolated. */
function attempt(fn) {
  try { fn(); } catch (_) { /* not in Telegram, or an older client */ }
}

/* A one-time ?t= link from the bot. Exchanged for a session and stripped from
   the address bar immediately, so it never lands in history or a screenshot. */
export async function claimLoginLink() {
  const url = new URL(location.href);
  const t = url.searchParams.get('t');
  if (!t) return;
  url.searchParams.delete('t');
  history.replaceState({}, '', url);
  try {
    const r = await fetch('/api/panel/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ t }),
    });
    const d = await r.json();
    if (d.session) session.set(d.session);   // persists across tabs/restarts
  } catch (_) { /* the screen's own error handling reports it */ }
}

export function boot() {
  if (!tg) return;
  attempt(() => tg.ready());
  attempt(() => tg.expand());
  attempt(() => tg.setHeaderColor('secondary_bg_color'));
  attempt(() => tg.enableClosingConfirmation());
}

export const inTelegram = () => Boolean(tg?.initData);

export function haptic(type = 'light') {
  try {
    if (type === 'ok') tg?.HapticFeedback?.notificationOccurred('success');
    else if (type === 'err') tg?.HapticFeedback?.notificationOccurred('error');
    else tg?.HapticFeedback?.impactOccurred(type);
  } catch (_) {}
}

export function authQuery() {
  if (tg?.initData) return `_auth=${encodeURIComponent(tg.initData)}`;
  const s = session.get();
  return s ? `_session=${encodeURIComponent(s)}` : `token=${devToken}`;
}

/* One lifetime now: a session survives closing the tab and restarting the
   browser, and the server slides its expiry forward while it is in use, so an
   admin stays signed in. The trade-off is that anyone with the device is
   signed in too — Sign out under More is the way off a shared machine.
   Pass tabOnly to keep a session confined to one tab. */
export const session = {
  get: () => sessionStorage.getItem('session') || localStorage.getItem('session') || '',
  set: (v, tabOnly = false) => (tabOnly ? sessionStorage : localStorage)
    .setItem('session', v),
  clear: () => {
    sessionStorage.removeItem('session');
    localStorage.removeItem('session');
  },
};

// Anyone signed in before this change has a token in sessionStorage, and get()
// prefers that store — so the old tab-scoped token would shadow the persistent
// one and they would be signed out again the next time the tab closed. Move it
// across once, on load, so the upgrade doesn't cost a sign-in.
try {
  const stale = sessionStorage.getItem('session');
  if (stale) {
    localStorage.setItem('session', stale);
    sessionStorage.removeItem('session');
  }
} catch (_) { /* private mode with storage disabled — nothing to migrate */ }

export async function api(path, opts = {}) {
  const headers = { 'Content-Type': 'application/json' };
  if (tg?.initData) headers['X-Init-Data'] = tg.initData;
  if (devToken) headers['X-Admin-Token'] = devToken;
  const s = session.get();
  if (s) headers['X-Session'] = s;
  const res = await fetch(`/api${path}`, {
    ...opts, headers,
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  let data = null;
  try { data = await res.json(); } catch (_) {}
  // The server slides the expiry forward on a session that's in use and hands
  // the new token back on this header. Swapping it in here means every call
  // keeps the session alive, so an admin who uses the panel is never signed
  // out. Kept in whichever store the current one lives in, so a tab-scoped
  // session isn't quietly promoted to a persistent one by a renewal.
  const fresh = res.headers.get('X-Session-Renew');
  if (fresh) session.set(fresh);
  if (!res.ok) throw new Error(data?.error || describe(res.status));
  return data;
}

function describe(status) {
  if (status === 401) return 'Open this from the bot to sign in.';
  if (status === 403) return 'This account does not have access.';
  if (status === 404) return 'That is no longer here.';
  return 'Something went wrong. Try again.';
}

let toastTimer;
export function toast(msg, kind = '') {
  let el = document.querySelector('.toast');
  if (!el) { el = document.createElement('div'); el.className = 'toast'; document.body.append(el); }
  el.textContent = msg;
  el.className = `toast show ${kind}`;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.remove('show'), 2600);
  haptic(kind === 'err' ? 'err' : 'ok');
}

export async function copy(text, label = 'Copied') {
  try {
    await navigator.clipboard.writeText(text);
    toast(label);
  } catch (_) {
    toast('Select and copy manually', 'err');
  }
}

/* Back-button stack: every push() adds a screen, the Telegram back arrow pops it. */
const stack = [];
export function pushScreen(render) {
  stack.push(render);
  syncBack();
  render();
}
export function replaceScreen(render) {
  stack.length = 0;
  stack.push(render);
  syncBack();
  render();
}
export function popScreen() {
  if (stack.length > 1) { stack.pop(); syncBack(); stack[stack.length - 1](); }
}
export function refresh() { stack[stack.length - 1]?.(); }
function syncBack() {
  if (!tg?.BackButton) return;
  attempt(() => (stack.length > 1 ? tg.BackButton.show() : tg.BackButton.hide()));
}
attempt(() => tg.BackButton.onClick(() => popScreen()));

export const esc = (s) => String(s ?? '').replace(/[&<>"']/g,
  (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

/* Display currency. Rate is presentational only — every order is priced,
   charged and settled in the shop's primary currency. */
let CUR = { symbol: '', rate: 1, decimals: 2 };
export function setSymbol(s) { CUR = { ...CUR, symbol: s }; }
export function setCurrency(c) { CUR = { ...CUR, ...c }; }
export const currency = () => CUR;
export const money = (n) => CUR.symbol + (Number(n || 0) * CUR.rate).toLocaleString(
  undefined, { minimumFractionDigits: CUR.decimals, maximumFractionDigits: CUR.decimals });

export function gauge(stock, infinite, threshold = 3) {
  if (infinite) return `<div class="gauge infinite">${'<i></i>'.repeat(12)}</div>`;
  const slots = 20;
  const filled = Math.min(slots, stock);
  const tone = stock === 0 ? 'out' : stock <= threshold ? 'warn' : 'on';
  let html = '';
  for (let i = 0; i < slots; i++) {
    const on = i < filled ? tone : '';
    const notch = i === threshold ? ' notch' : '';
    html += `<i class="${on}${notch}"></i>`;
  }
  return `<div class="gauge">${html}</div>`;
}

export const STATUS = {
  delivered:       { label: 'Delivered', cls: 'ok' },
  paid:            { label: 'Paid',      cls: 'ok' },
  pending:         { label: 'Awaiting payment', cls: 'wait' },
  awaiting_review: { label: 'In review', cls: 'wait' },
  cancelled:       { label: 'Cancelled', cls: 'neutral' },
  expired:         { label: 'Expired',   cls: 'neutral' },
  rejected:        { label: 'Rejected',  cls: 'bad' },
};
export const statusPill = (s) => {
  const m = STATUS[s] || { label: s, cls: 'neutral' };
  return `<span class="pill ${m.cls}">${m.label}</span>`;
};
