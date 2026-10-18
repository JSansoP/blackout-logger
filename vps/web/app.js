const RANGES = {
  7: { rowHeight: 22, gap: 4, label: "weekday" },
  30: { rowHeight: 12, gap: 3, label: "weekday" },
  90: { rowHeight: 5, gap: 2, label: "monday" },
};
const REFRESH_MS = 60_000;
const DAY_MS = 86_400_000;
const TYPE_NAMES = { power: "Power", internet: "Internet", stuck: "Pi stuck", unknown: "Unknown", ongoing: "Ongoing" };
const COUNT_NAMES = { power: "power", internet: "internet", stuck: "Pi stuck", unknown: "unknown", ongoing: "ongoing" };
const STATE_WORDS = { online: "Online", offline: "Offline", stuck: "Probably stuck", no_data: "No data" };

const state = { days: Number(readPref("days", "30")) || 30, status: null, blackouts: null, error: null };
if (!RANGES[state.days]) state.days = 30;

const $ = (id) => document.getElementById(id);

function readPref(key, fallback) {
  try {
    return localStorage.getItem(key) ?? fallback;
  } catch {
    return fallback;
  }
}

function writePref(key, value) {
  try {
    if (value === null) localStorage.removeItem(key);
    else localStorage.setItem(key, value);
  } catch {}
}

function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
}

async function api(path) {
  let response;
  try {
    response = await fetch(`api/${path}`, { headers: { Accept: "application/json" } });
  } catch {
    throw new Error("Cannot reach the dashboard server. Check the blackout-web service on the VPS.");
  }
  const body = await response.json().catch(() => null);
  if (!response.ok) throw new Error(body?.error || `Request failed with status ${response.status}`);
  return body;
}

/* Formatting */

const timeFormat = new Intl.DateTimeFormat(undefined, { hour: "2-digit", minute: "2-digit" });
const dateTimeFormat = new Intl.DateTimeFormat(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
const fullFormat = new Intl.DateTimeFormat(undefined, { year: "numeric", month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
const weekdayFormat = new Intl.DateTimeFormat(undefined, { weekday: "short", day: "numeric" });
const dayFormat = new Intl.DateTimeFormat(undefined, { month: "short", day: "numeric" });

const toDate = (iso) => (iso ? new Date(iso) : null);

function startOfDay(date) {
  const d = new Date(date);
  d.setHours(0, 0, 0, 0);
  return d;
}

function addDays(date, n) {
  const d = new Date(date);
  d.setDate(d.getDate() + n);
  return d;
}

function formatWhen(date) {
  if (!date) return "–";
  const today = startOfDay(new Date());
  if (date >= today) return `today ${timeFormat.format(date)}`;
  if (date >= addDays(today, -1)) return `yesterday ${timeFormat.format(date)}`;
  if (date.getFullYear() !== today.getFullYear()) return fullFormat.format(date);
  return dateTimeFormat.format(date);
}

function formatDuration(seconds) {
  if (seconds == null) return "–";
  const s = Math.max(0, Math.round(seconds));
  if (s < 60) return `${s} s`;
  const min = Math.round(s / 60);
  if (min < 60) return `${min} min`;
  const h = Math.floor(min / 60);
  if (h < 24) return `${h} h ${String(min % 60).padStart(2, "0")} min`;
  return `${Math.floor(h / 24)} d ${h % 24} h`;
}

function splitAgo(date, now) {
  const minutes = Math.floor((now - date) / 60_000);
  if (minutes < 1) return ["Now", ""];
  if (minutes < 60) return [String(minutes), "min ago"];
  const hours = Math.floor(minutes / 60);
  if (hours < 48) return [String(hours), hours === 1 ? "hour ago" : "hours ago"];
  const days = Math.floor(hours / 24);
  return [String(days), "days ago"];
}

function formatAgo(date, now) {
  const [value, unit] = splitAgo(date, now);
  return unit ? `${value} ${unit}` : "just now";
}

/* Outage intervals */

function outageType(b) {
  return b.resolved ? b.type : "ongoing";
}

function outageInterval(b, now) {
  const start = toDate(b.started_at);
  let end;
  if (!b.resolved) end = now;
  else if (b.type === "stuck") end = toDate(b.seen_again_at) ?? now;
  else end = toDate(b.ended_at) ?? start;
  return [start, end < start ? start : end];
}

function outageTitle(b, now) {
  const [start, end] = outageInterval(b, now);
  const type = TYPE_NAMES[outageType(b)];
  if (!b.resolved) return `${type} since ${formatWhen(start)}, ${formatDuration((now - start) / 1000)} so far`;
  if (b.type === "stuck") {
    const seen = b.seen_again_at ? `, seen again ${formatWhen(end)}` : ", not seen since";
    return `${type} from ${formatWhen(start)}${seen}`;
  }
  return `${type}, ${formatWhen(start)} to ${timeFormat.format(end)}, ${formatDuration(b.duration_seconds)}`;
}

/* Rendering */

function renderReadouts() {
  const { status, blackouts } = state;
  if (!status) return;
  const now = new Date(status.now);
  const since = toDate(status.since);
  const lastPoll = toDate(status.last_poll?.timestamp);

  let statusSub;
  if (status.state === "online") {
    statusSub = `Up <strong>${formatDuration((now - since) / 1000)}</strong>, since ${esc(formatWhen(since))}.`;
  } else if (status.state === "offline") {
    statusSub = `Unreachable since ${esc(formatWhen(since))}, <strong>${formatDuration((now - since) / 1000)}</strong> so far. Power or internet is down at home.`;
  } else if (status.state === "stuck") {
    statusSub = `Not seen since ${esc(formatWhen(since))}. The Pi may be stuck in standby after a power cut: press its power button.`;
  } else {
    statusSub = "No checks recorded yet. The checker runs every 10 minutes from cron.";
  }
  const stale = status.stale && lastPoll
    ? `<p class="stale">The last check was ${esc(formatAgo(lastPoll, now))}. The checker may not be running: see vps/blackout_checker.log on the VPS.</p>`
    : "";

  const last = status.latest_blackout;
  let lastValue = '<span class="num">None</span>';
  let lastSub = "No outages recorded yet.";
  if (last) {
    const [start] = outageInterval(last, now);
    const [value, unit] = last.resolved ? splitAgo(start, now) : ["Now", ""];
    lastValue = `<span class="num">${value}</span>${unit ? `<span class="unit">${unit}</span>` : ""}`;
    lastSub = esc(outageTitle(last, now)) + ".";
  }

  let countValue = '<span class="num">–</span>';
  let countSub = "";
  if (blackouts) {
    const counts = {};
    let total = 0;
    for (const b of blackouts) {
      const type = outageType(b);
      counts[type] = (counts[type] ?? 0) + 1;
      if (b.resolved && b.duration_seconds != null) total += b.duration_seconds;
    }
    const parts = Object.keys(TYPE_NAMES).filter((t) => counts[t]).map((t) => `${counts[t]} ${COUNT_NAMES[t]}`);
    countValue = `<span class="num">${blackouts.length}</span><span class="unit">${blackouts.length === 1 ? "outage" : "outages"}</span>`;
    countSub = blackouts.length
      ? `${esc(parts.join(", "))}. <strong>${formatDuration(total)}</strong> without service${counts.stuck || counts.ongoing ? ", not counting stuck or ongoing ones" : " in total"}.`
      : "No outages in this period.";
  }

  $("readouts").innerHTML = `
    <div class="readout readout-status">
      <h2 class="readout-label">Raspberry Pi at home</h2>
      <p class="state ${status.state}">${STATE_WORDS[status.state]}</p>
      <p class="readout-sub">${statusSub}</p>
      ${stale}
    </div>
    <div class="readout">
      <h2 class="readout-label">Last outage</h2>
      <p class="readout-value">${lastValue}</p>
      <p class="readout-sub">${lastSub}</p>
    </div>
    <div class="readout">
      <h2 class="readout-label">Last ${state.days} days</h2>
      <p class="readout-value">${countValue}</p>
      <p class="readout-sub">${countSub}</p>
    </div>`;

  $("checked").textContent = lastPoll ? `Checked ${formatAgo(lastPoll, now)}` : "";
}

function dayLabel(day, mode) {
  if (mode === "weekday") return weekdayFormat.format(day);
  return day.getDay() === 1 ? dayFormat.format(day) : "";
}

function renderMap() {
  const map = $("map");
  if (!state.blackouts || !state.status) return;
  const range = RANGES[state.days];
  const now = new Date(state.status.now);
  const today = startOfDay(now);
  const intervals = state.blackouts.map((b) => [b, ...outageInterval(b, now)]);

  map.style.setProperty("--row-h", `${range.rowHeight}px`);
  map.style.setProperty("--row-gap", `${range.gap}px`);

  const hours = [0, 6, 12, 18, 24].map((h) => `<span style="left:${(h / 24) * 100}%">${String(h).padStart(2, "0")}:00</span>`).join("");
  const rows = [`<div class="map-axis" aria-hidden="true"><div></div><div class="map-hours">${hours}</div></div>`];

  for (let i = 0; i < state.days; i++) {
    const dayStart = addDays(today, -i);
    const dayEnd = addDays(dayStart, 1);
    const span = dayEnd - dayStart;
    const marks = [];
    for (const [b, start, end] of intervals) {
      if (end < dayStart || start >= dayEnd) continue;
      const from = Math.max(start, dayStart);
      const to = Math.min(end, dayEnd);
      const left = ((from - dayStart) / span) * 100;
      const width = ((to - from) / span) * 100;
      marks.push(`<span class="mark type-${outageType(b)}" style="left:${left.toFixed(3)}%;width:${width.toFixed(3)}%" title="${esc(outageTitle(b, now))}"></span>`);
    }
    const future = i === 0 ? `<span class="map-future" style="left:${(((now - dayStart) / span) * 100).toFixed(3)}%" title="Now"></span>` : "";
    const weekend = dayStart.getDay() === 0 || dayStart.getDay() === 6 ? " weekend" : "";
    rows.push(`<div class="map-row"><span class="map-day${weekend}">${esc(dayLabel(dayStart, range.label))}</span><div class="map-track">${marks.join("")}${future}</div></div>`);
  }
  map.innerHTML = rows.join("");
  map.setAttribute("role", "img");
  map.setAttribute("aria-label", `${state.blackouts.length} outages in the last ${state.days} days, drawn by day and time of day. The table below lists them.`);
}

function renderList() {
  const list = $("list");
  if (!state.blackouts || !state.status) return;
  const now = new Date(state.status.now);
  if (!state.blackouts.length) {
    list.innerHTML = `<div class="empty"><h2>No outages in the last ${state.days} days</h2><p>Every check reached the Pi. Pick a longer range to see older outages.</p></div>`;
    return;
  }
  const rows = state.blackouts.map((b) => {
    const type = outageType(b);
    const [start, end] = outageInterval(b, now);
    let ended;
    let duration;
    if (!b.resolved) {
      ended = '<td class="ongoing">Ongoing</td>';
      duration = `<td class="r">${formatDuration((now - start) / 1000)} so far</td>`;
    } else if (b.type === "stuck") {
      ended = `<td class="muted">${b.seen_again_at ? `Unknown, seen again ${esc(formatWhen(end))}` : "Unknown, not seen since"}</td>`;
      duration = '<td class="r muted">Unknown</td>';
    } else {
      ended = `<td>${esc(formatWhen(end))}</td>`;
      duration = `<td class="r">${formatDuration(b.duration_seconds)}</td>`;
    }
    return `<tr><td>${esc(formatWhen(start))}</td>${ended}${duration}<td><i class="swatch type-${type}"></i>${TYPE_NAMES[type]}</td></tr>`;
  });
  list.innerHTML = `<table class="data-table">
    <thead><tr><th scope="col">Started</th><th scope="col">Ended</th><th scope="col" class="r">Duration</th><th scope="col">Type</th></tr></thead>
    <tbody>${rows.join("")}</tbody>
  </table>`;
}

function renderError() {
  const existing = document.querySelector(".error");
  existing?.remove();
  if (!state.error) return;
  const p = document.createElement("p");
  p.className = "error";
  p.setAttribute("role", "alert");
  p.textContent = state.error;
  $("view").prepend(p);
}

function renderRange() {
  $("range-title").textContent = `Last ${state.days} days`;
  document.querySelectorAll("#range button").forEach((b) => b.setAttribute("aria-pressed", String(Number(b.dataset.days) === state.days)));
}

function render() {
  renderRange();
  renderError();
  renderReadouts();
  renderMap();
  renderList();
}

async function refresh() {
  try {
    const [status, list] = await Promise.all([api("status"), api(`blackouts?days=${state.days}`)]);
    state.status = status;
    state.blackouts = list.days === state.days ? list.blackouts : state.blackouts;
    state.error = null;
  } catch (e) {
    state.error = e.message;
  }
  render();
}

/* Events */

$("range").addEventListener("click", (event) => {
  const button = event.target.closest("button[data-days]");
  if (!button) return;
  state.days = Number(button.dataset.days);
  writePref("days", String(state.days));
  renderRange();
  refresh();
});

$("theme-button").addEventListener("click", () => {
  const root = document.documentElement;
  const current = root.dataset.theme ?? (matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
  const next = current === "dark" ? "light" : "dark";
  root.dataset.theme = next;
  writePref("theme", next);
});

document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "visible") refresh();
});

renderRange();
refresh();
setInterval(() => {
  if (document.visibilityState === "visible") refresh();
}, REFRESH_MS);
