const refreshSeconds = 30;
let remaining = refreshSeconds;

const $ = (id) => document.getElementById(id);

function value(v, fallback = 'N/A') {
  return v === null || v === undefined || v === '' ? fallback : v;
}

function number(v, digits = 4) {
  if (v === null || v === undefined || v === '') return 'N/A';
  if (v === 'inf') return 'inf';
  const n = Number(v);
  if (Number.isNaN(n)) return String(v);
  return n.toFixed(digits);
}

function pct(v) {
  if (v === null || v === undefined || v === '') return 'N/A';
  return `${number(v, 2)}%`;
}

function classForStatus(text) {
  const normalized = String(text).toUpperCase();
  if (normalized === 'OK' || normalized === 'ONLINE' || normalized === 'TRUE') return 'ok';
  if (normalized === 'WARNING') return 'warn';
  return 'bad';
}

function setText(id, text, statusClass = null) {
  const el = $(id);
  el.textContent = value(text);
  el.className = statusClass || '';
}

async function getJSON(path) {
  const res = await fetch(path, { cache: 'no-store' });
  if (!res.ok) throw new Error(`${path}: ${res.status}`);
  return res.json();
}

function renderStatus(data) {
  setText('bot-status', data.bot, classForStatus(data.bot));
  setText('guardian-status', data.guardian, classForStatus(data.guardian));
  setText('last-execution', data.last_execution);
  setText('last-snapshot', data.last_snapshot);
  setText('last-healthcheck', data.last_healthcheck);
  setText('last-preflight', data.last_preflight);
}

function renderMetrics(data) {
  setText('capital-current', number(data.capital_current));
  setText('spot-capital-limit', number(data.spot_capital_limit_usdt, 2));
  setText('futures-capital-limit', number(data.futures_capital_limit_usdt, 2));
  setText('max-exposure-percent', pct(data.max_exposure_percent));
  setText('pnl-total', number(data.pnl_total));
  setText('win-rate', pct(data.win_rate));
  setText('profit-factor', value(data.profit_factor));
  setText('open-trades', data.open_trades);
  setText('closed-trades', data.closed_trades);
  setText('long-win-rate', pct(data.long_win_rate));
  setText('short-win-rate', pct(data.short_win_rate));
}

function renderHealth(data) {
  setText('healthcheck', data.healthcheck, classForStatus(data.healthcheck));
  setText('observability', data.observability, classForStatus(data.observability));
  setText('json-corrupt', data.json_corrupt_lines);
  setText('state-alignment', data.state_alignment, classForStatus(data.state_alignment));
  setText('last-error', data.last_error || 'N/A');
}

function renderTrades(data) {
  const body = $('trades-body');
  const rows = data.trades || [];
  body.innerHTML = rows.length ? rows.map((t) => `
    <tr>
      <td>${value(t.time)}</td>
      <td>${value(t.symbol)}</td>
      <td>${value(t.side)}</td>
      <td>${value(t.result)}</td>
      <td>${number(t.pnl_usdt)}</td>
      <td>${value(t.exit_reason)}</td>
    </tr>
  `).join('') : '<tr><td colspan="6">Sin trades cerrados</td></tr>';
}

function renderSnapshots(data) {
  const body = $('snapshots-body');
  const rows = data.snapshots || [];
  body.innerHTML = rows.length ? rows.map((s) => `
    <tr>
      <td>${value(s.timestamp)}</td>
      <td>${value(s.market_regime)}</td>
      <td>${value(s.candidates, 0)}</td>
      <td>${value(s.accepted, 0)}</td>
      <td>${value(s.rejected, 0)}</td>
      <td>${value(s.skipped, 0)}</td>
    </tr>
  `).join('') : '<tr><td colspan="6">Sin snapshots</td></tr>';
}

async function refresh() {
  try {
    const [status, metrics, health, trades, snapshots] = await Promise.all([
      getJSON('/api/status'),
      getJSON('/api/metrics'),
      getJSON('/api/health'),
      getJSON('/api/trades'),
      getJSON('/api/snapshots'),
    ]);
    renderStatus(status);
    renderMetrics(metrics);
    renderHealth(health);
    renderTrades(trades);
    renderSnapshots(snapshots);
  } catch (err) {
    setText('last-error', err.message || String(err));
  }
}

setInterval(() => {
  remaining -= 1;
  if (remaining <= 0) {
    remaining = refreshSeconds;
    refresh();
  }
  $('refresh-count').textContent = remaining;
}, 1000);

refresh();
