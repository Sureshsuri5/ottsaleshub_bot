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
    if (d.session) session.set(d.session);
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

export const session = {
  get: () => localStorage.getItem('session') || '',
  set: (v) => localStorage.setItem('session', v),
  clear: () => localStorage.removeItem('session'),
};

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
