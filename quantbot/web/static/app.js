/* QuantBot dashboard: polls /api/state and renders. No framework. */
(function () {
  const { lineChart, esc, css } = window.Charts;
  const $ = (s) => document.querySelector(s);
  const SIGNALS = [
    ['trend', 'Trend'], ['momentum', 'Momentum'], ['mean_reversion', 'Mean reversion'], ['breakout', 'Breakout'],
    ['volume', 'Volume pressure'], ['rel_strength', 'Relative strength'], ['setups', 'Chart setups'],
  ];
  const ACTIONS = {
    BUY: ['buy', '▲', 'Buy'], SHORT: ['short', '▼', 'Short'], SELL: ['exit', '◆', 'Sell'], COVER: ['exit', '◆', 'Cover'],
    HOLD: ['', '●', 'Hold'], WAIT: ['quiet', '○', 'Wait'], SKIP: ['quiet', '⊘', 'Skipped'], ERROR: ['exit', '!', 'No data'],
  };
  const ORDER = { BUY: 0, SHORT: 0, SELL: 0, COVER: 0, HOLD: 1, SKIP: 2, WAIT: 3, ERROR: 4 };
  const SETUP_NAMES = {
    breakout_20d: '20-day breakout', bull_flag: 'bull flag', reclaim_ma20: 'reclaimed 20-day average',
    oversold_bounce: 'oversold bounce', breakdown_20d: '20-day breakdown', bear_flag: 'bear flag',
    lose_ma20: 'lost 20-day average', overbought_fade: 'overbought fade', orb_up: 'opening-range break up',
    orb_down: 'opening-range break down',
  };

  let S = null, BT = null;
  const filters = { cls: 'all', show: 'all' };
  const open = new Set();
  const seen = {};

  // ---- formatting ----
  const isNum = (v) => v != null && isFinite(v);
  const usd = (v, d = 2) => !isNum(v) ? '–' : (v < 0 ? '−$' : '$') + Math.abs(v).toLocaleString('en-US', { minimumFractionDigits: d, maximumFractionDigits: d });
  const signedUsd = (v, d = 2) => !isNum(v) ? '–' : (v > 0 ? '+' : '') + usd(v, d);
  const num = (v, d = 2) => !isNum(v) ? '–' : v.toLocaleString('en-US', { minimumFractionDigits: d, maximumFractionDigits: d });
  const signed = (v, d = 2) => !isNum(v) ? '–' : (v > 0 ? '+' : v < 0 ? '−' : '') + Math.abs(v).toFixed(d);
  const pct = (v, d = 1) => !isNum(v) ? '–' : v.toFixed(d) + '%';
  const signedPct = (v, d = 2) => !isNum(v) ? '–' : signed(v, d) + '%';
  const price = (v) => !isNum(v) ? '–' : num(v, Math.abs(v) < 5 ? 4 : 2);
  const qty = (v) => !isNum(v) ? '–' : (+v.toFixed(6)).toString();
  const tone = (v) => !isNum(v) || v === 0 ? '' : v > 0 ? 'up' : 'down';
  const when = (iso) => !iso ? '–' : new Date(iso).toLocaleString(undefined, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' });
  const ago = (iso) => {
    if (!iso) return '';
    const m = Math.round((Date.now() - new Date(iso)) / 60000);
    return m < 1 ? 'just now' : m < 60 ? `${m} min ago` : m < 1440 ? `${Math.round(m / 60)} h ago` : `${Math.round(m / 1440)} d ago`;
  };

  function changed(key, data) {
    const j = JSON.stringify(data);
    if (seen[key] === j) return false;
    seen[key] = j;
    return true;
  }

  function chip(action) {
    const [cls, glyph, label] = ACTIONS[action] || ['', '', action];
    return `<span class="chip ${cls}"><span aria-hidden="true">${glyph}</span>${esc(label)}</span>`;
  }

  function meter(v, thr, canShort) {
    if (!isNum(v)) return '<span class="k">no reading</span>';
    const w = Math.min(Math.abs(v), 1) * 50;
    const ticks = `<span class="thr" style="left:${50 + thr * 50}%"></span>` + (canShort ? `<span class="thr" style="left:${50 - thr * 50}%"></span>` : '');
    return `<div class="conv"><span class="v num">${signed(v)}</span><div class="meter" title="Conviction ${signed(v)}; entry at ±${thr}">
      <span class="track"></span><span class="zero"></span>${ticks}
      <span class="bar ${v >= 0 ? 'pos' : 'neg'}" style="width:${w}%"></span></div></div>`;
  }

  // ---- header ----
  function renderHeader() {
    const mode = $('#mode');
    mode.textContent = { 'dry-run': 'DRY-RUN · simulated money', paper: 'PAPER · Alpaca paper account', live: 'LIVE · real money' }[S.mode] || S.mode;
    mode.className = 'badge ' + (S.mode === 'live' ? 'live' : '');
    $('#market').innerHTML = S.market_open == null ? '' :
      `<span class="dot ${S.market_open ? 'on' : ''}"></span>US market ${S.market_open ? 'open' : 'closed' + (S.next_open ? ` · opens ${when(S.next_open)}` : '')}`;
    $('#last-run').textContent = S.cycle ? `${when(S.cycle.ts)} (${ago(S.cycle.ts)})` : 'none yet';
    const btn = $('#run-now');
    btn.disabled = S.busy || !S.trading_enabled;
    btn.textContent = S.busy ? 'Analysing…' : 'Run analysis now';

    const alerts = [];
    if (S.mode === 'live') alerts.push(['bad', 'Live mode: orders use real money.']);
    if (!S.trading_enabled) alerts.push(['', 'Started with --no-trading: the dashboard is read-only and no cycles run.']);
    if (S.broker_error) alerts.push(['bad', `Broker error: ${S.broker_error}`]);
    if (S.last_error) alerts.push(['bad', `Last cycle failed: ${S.last_error}`]);
    const sum = S.cycle && S.cycle.summary;
    if (sum && sum.kill_switch) alerts.push(['bad', `Kill switch tripped: day P&L ${signedPct(sum.day_pnl_pct)} hit the −${S.limits.max_daily_loss_pct}% limit. No new entries today.`]);
    if (sum && Object.keys(sum.errors || {}).length) alerts.push(['', `No data this cycle for: ${Object.keys(sum.errors).join(', ')}.`]);
    $('#alerts').innerHTML = alerts.map(([c, t]) => `<div class="alert ${c}">${esc(t)}</div>`).join('');
  }

  function tick() {
    if (!S) return;
    const el = $('#next-run');
    if (!S.trading_enabled) { el.textContent = 'off'; return; }
    if (S.busy) { el.textContent = 'running now'; return; }
    if (!S.next_run) { el.textContent = '–'; return; }
    const s = Math.max(0, Math.round((new Date(S.next_run) - Date.now()) / 1000));
    el.textContent = `${when(S.next_run)} (in ${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')})`;
  }

  // ---- tiles ----
  function renderTiles() {
    const a = S.account, st = S.stats || {};
    if (!a) { $('#tiles').innerHTML = '<div class="tile"><div class="label">Account</div><div class="value">unavailable</div></div>'; return; }
    const tile = (label, value, delta = '', cls = '') => `<div class="tile ${cls}"><div class="label">${label}</div><div class="value">${value}</div><div class="delta">${delta}</div></div>`;
    $('#tiles').innerHTML = [
      tile('Account value', usd(a.equity), `<span class="${tone(a.total_pnl)}">${signedUsd(a.total_pnl)}</span> since the bot started at ${usd(a.start_equity, 0)}`, 'hero'),
      tile('Today', `<span class="${tone(a.day_pnl)}">${signedUsd(a.day_pnl)}</span>`, `${signedPct(a.day_pnl_pct)} · kill switch at −${S.limits.max_daily_loss_pct}%`),
      tile('Cash', usd(a.cash, 0), `${pct(a.equity ? a.cash / a.equity * 100 : 0, 0)} of account`),
      tile('Invested', pct(a.gross_pct, 0), `${usd(a.gross_exposure, 0)} in ${S.positions.length} position${S.positions.length === 1 ? '' : 's'} · max ${S.limits.max_gross_exposure_pct}%`),
      tile('Win rate', st.trades ? pct(st.win_rate * 100, 0) : '–', st.trades ? `${st.trades} closed trade${st.trades === 1 ? '' : 's'}` : 'no closed trades yet'),
      tile('Average trade', st.trades ? signed(st.avg_r) + 'R' : '–', st.trades ? `profit factor ${st.profit_factor == null ? '–' : num(st.profit_factor)}` : '1R = the amount risked'),
    ].join('');
  }

  // ---- equity chart ----
  function renderEquity() {
    const rows = S.equity_curve || [];
    $('#equity-sub').textContent = rows.length ? `Recorded after every analysis since ${when(rows[0].ts)}.` : '';
    lineChart($('#equity-chart'), {
      title: 'Account value over time',
      labels: rows.map((r) => r.ts),
      series: [{ name: 'Account value', color: css('--s1'), values: rows.map((r) => r.equity) }],
      fmt: (v) => usd(v, 0), xFmt: (l) => when(l), tipFmt: (l) => when(l), xName: 'Time',
      empty: 'The curve appears after the second analysis cycle.',
    });
  }

  // ---- regime & weights ----
  function renderBackdrop() {
    const sum = (S.cycle && S.cycle.summary) || {};
    const regime = sum.regime || {};
    const word = (v) => !isNum(v) ? 'no reading' : v > 0.4 ? 'Friendly to longs' : v > 0.1 ? 'Mildly friendly' : v > -0.1 ? 'Neutral' : v > -0.4 ? 'Mildly hostile' : 'Hostile to longs';
    $('#regime').innerHTML = ['stocks', 'crypto'].map((ac) =>
      `<div class="regime-row"><span>${ac === 'stocks' ? 'Stocks' : 'Crypto'}</span>${meter(regime[ac], 0, false).replace(/<span class="thr"[^>]*><\/span>/g, '')}<span class="k">${word(regime[ac])}</span></div>`).join('');
    const fits = sum.fits || {};
    const classes = Object.keys(fits);
    if (!classes.length) { $('#weights').innerHTML = '<div class="empty">Shown after the first analysis.</div>'; return; }
    let h = '<table class="tbl"><thead><tr><th>Signal</th>' + classes.map((c) => `<th class="num">${c} weight</th><th class="num">${c} IC</th>`).join('') + '</tr></thead><tbody>';
    SIGNALS.forEach(([k, name]) => {
      h += `<tr><td>${name}</td>` + classes.map((c) => `<td class="num">${signed(fits[c].weights[k])}</td><td class="num">${isNum((fits[c].ic || {})[k]) ? signed(fits[c].ic[k], 3) : '–'}</td>`).join('') + '</tr>';
    });
    h += '</tbody></table><p class="note">IC = rank correlation between the signal and the next two days\' return in the fitting window. ' +
      classes.map((c) => `${c}: ${fits[c].has_edge ? 'fitted' : 'no usable edge found, entries off'}${fits[c].fitted_ts ? ' ' + when(fits[c].fitted_ts) : ''}`).join(' · ') + '.</p>';
    $('#weights').innerHTML = h;
  }

  // ---- decisions ----
  function leanText(d) {
    const x = d.detail || {}, nb = x.next_bar || {};
    let t = esc(x.lean || '–');
    if (isNum(nb.p_up)) t += `<div class="k">next hour up ${pct(nb.p_up * 100, 0)} of ${nb.n} similar readings</div>`;
    return t;
  }

  function detailHtml(d) {
    const x = d.detail || {};
    if (!x.signals) return `<p class="note">${esc(d.reason)}</p>`;
    let sig = '<div class="sig"><span class="hd">Signal</span><span class="hd">Reading</span><span class="hd num">Value</span><span class="hd num">Weight</span><span class="hd num">Adds</span>';
    SIGNALS.forEach(([k, name]) => {
      const v = x.signals[k] ?? 0, w = (x.weights || {})[k] ?? 0;
      sig += `<span>${name}</span><span class="track" title="${name}: ${signed(v)}"><span class="bar ${v >= 0 ? 'pos' : 'neg'}" style="width:${Math.min(Math.abs(v), 1) * 50}%"></span></span>
        <span class="num">${signed(v)}</span><span class="num">${signed(w)}</span><span class="num">${signed(v * w, 3)}</span>`;
    });
    sig += '</div><div class="legend"><span><i style="background:var(--bull)"></i>bullish reading</span><span><i style="background:var(--bear)"></i>bearish reading</span></div>';
    const setups = (x.setups || []).map((s) => SETUP_NAMES[s] || s).join(', ') || 'none';
    const nb = x.next_bar || {};
    sig += `<dl class="kv" style="margin-top:10px">
      <dt>Conviction</dt><dd>${signed(d.conviction)} (entry at ±${x.threshold})</dd>
      <dt>Market regime</dt><dd>${signed(x.regime)}</dd>
      <dt>RSI (14 h)</dt><dd>${num(x.rsi, 1)}</dd>
      <dt>Size multiplier</dt><dd>×${num(x.vol_mult)} (lower when volatility is elevated)</dd>
      <dt>Chart setups</dt><dd>${esc(setups)}</dd>
      <dt>Next hour</dt><dd>${isNum(nb.p_up) ? `closed up ${pct(nb.p_up * 100, 0)} of the time after ${nb.n} similar readings (avg ${signedPct(nb.avg_ret_pct, 3)})` : 'too few similar readings'}</dd>
    </dl>`;

    let h = `<div class="detail-grid"><div class="panel"><h4>Analysis</h4>${sig}</div>`;

    const p = x.plan, tm = x.timemachine, pos = x.position;
    if (pos) {
      h += `<div class="panel"><h4>Open position</h4><dl class="kv">
        <dt>Side</dt><dd>${esc(pos.side)}</dd><dt>Quantity</dt><dd>${qty(pos.qty)}</dd>
        <dt>Entry</dt><dd>${price(pos.entry)}</dd><dt>Stop</dt><dd>${price(pos.stop)}</dd><dt>Target</dt><dd>${price(pos.target)}</dd>
        <dt>Unrealised</dt><dd class="${tone(pos.unrealized)}">${signedUsd(pos.unrealized)}</dd>
        <dt>Held</dt><dd>${pos.bars_held} of ${pos.max_hold_bars} bars</dd></dl>
        ${x.closed ? `<p class="note">Closed at ${price(x.closed.exit_price)}: ${signedUsd(x.closed.pnl)} (${signed(x.closed.r_multiple)}R).</p>` : ''}</div>`;
    }
    if (p) {
      h += `<div class="panel"><h4>Plan and Time Machine</h4><dl class="kv">
        <dt>Trade</dt><dd>${p.side === 'long' ? 'Buy' : 'Short'} ${qty(x.qty)} @ ${price(p.entry)}</dd>
        <dt>Stop</dt><dd>${price(p.stop)} (${pct(p.risk_pct, 2)} away)</dd>
        <dt>Target</dt><dd>${price(p.target)} (${num(p.target_r, 1)}R)</dd>
        <dt>Loss at stop</dt><dd>${usd((x.qty || 0) * p.risk_per_unit)}</dd></dl>`;
      if (tm && tm.ok) {
        const part = (cls, v, label) => v <= 0 ? '' : `<div class="${cls}" style="flex:${v}" title="${label}: ${pct(v * 100, 1)}">${v >= 0.09 ? pct(v * 100, 0) : ''}</div>`;
        h += `<h4 style="margin-top:12px">Odds over the next ${tm.horizon_bars} bars</h4>
          <div class="odds" role="img" aria-label="Target first ${pct(tm.p_target * 100, 0)}, stop first ${pct(tm.p_stop * 100, 0)}, neither ${pct(tm.p_open * 100, 0)}">
          ${part('t', tm.p_target, 'Target first')}${part('s', tm.p_stop, 'Stop first')}${part('o', tm.p_open, 'Neither')}</div>
          <div class="legend"><span><i style="background:var(--good)"></i>✓ target first</span><span><i style="background:var(--critical)"></i>✕ stop first</span><span><i style="background:var(--axis)"></i>neither</span></div>
          <p class="note"><span class="verdict">${esc(tm.verdict)}.</span> Edge ${signed(tm.edge_points, 1)} points over the ${pct(tm.breakeven * 100, 0)} break-even; average ${signed(tm.net_r)}R after costs.
          ${tm.replays.toLocaleString()} replays from ${tm.analogs.toLocaleString()} ${tm.conditional ? 'past bars with a similar reading' : 'past bars (too few similar readings)'}, seed ${tm.seed}.
          Those bars really went: target ${pct(tm.history.p_target * 100, 0)}, stop ${pct(tm.history.p_stop * 100, 0)}. Simulated from history, not a prediction.</p>
          <div class="chart fan" data-sym="${esc(d.symbol)}"></div>`;
      } else if (tm) {
        h += `<p class="note">Time Machine: ${esc(tm.reason || tm.verdict || 'not run')}.</p>`;
      }
      h += '</div>';
    }
    const r = x.risk;
    if (r) {
      h += `<div class="panel"><h4>Risk gate: <span class="verdict">${r.passed ? '✓ PASS' : '✕ BLOCK'}</span></h4><ul class="checks">` +
        r.checks.map((c) => `<li><span aria-hidden="true">${c.ok ? '✓' : '✕'}</span><span>${esc(c.name)}${c.ok ? '' : ' (failed)'}</span><span class="d">${esc(c.detail)}</span></li>`).join('') + '</ul></div>';
    }
    h += '</div>';
    if (x.trigger) h = `<p class="note" style="margin:0 0 10px"><b>Next:</b> ${esc(x.trigger)}</p>` + h;
    return h;
  }

  function drawFans() {
    document.querySelectorAll('.chart.fan').forEach((host) => {
      const d = S.decisions.find((q) => q.symbol === host.dataset.sym);
      const tm = d && d.detail.timemachine, p = d && d.detail.plan;
      if (!tm || !tm.bands || !p) return;
      const b = tm.bands, n = b['50'].length;
      const lead = (arr) => [p.entry].concat(arr);
      lineChart(host, {
        title: 'Simulated price paths', height: 200, table: false,
        labels: Array.from({ length: n + 1 }, (_, i) => i),
        series: [{ name: 'Median replay', color: css('--s1'), values: lead(b['50']) }],
        bands: [
          { name: '5th–95th percentile', lo: lead(b['5']), hi: lead(b['95']), color: css('--s1'), opacity: 0.10 },
          { name: '25th–75th percentile', lo: lead(b['25']), hi: lead(b['75']), color: css('--s1'), opacity: 0.16 },
        ],
        hlines: [
          { value: p.target, label: '✓ Target', color: css('--good') },
          { value: p.stop, label: '✕ Stop', color: css('--critical') },
        ],
        legend: '<span><i style="background:var(--s1);opacity:.3"></i>middle half / 90% of replays</span>',
        fmt: price, xFmt: (l) => l === 0 ? 'now' : `+${l} bars`, tipFmt: (l) => l === 0 ? 'Now' : `${l} bars ahead`,
        xTicks: [0, Math.floor(n / 2), n],
      });
    });
  }

  function renderDecisions() {
    const sum = (S.cycle && S.cycle.summary) || {};
    const counts = sum.counts || {};
    const c = (k) => counts[k] || 0;
    $('#decisions-sub').textContent = S.cycle
      ? `Analysed ${when(S.cycle.ts)} on ${S.data_source} data: ${c('BUY')} bought, ${c('SHORT')} shorted, ${c('SELL') + c('COVER')} closed, ${c('HOLD')} held, ${c('SKIP')} skipped, ${c('WAIT')} waiting. Click a row for the full reasoning.`
      : 'No analysis yet.';
    const rows = (S.decisions || []).filter((d) =>
      (filters.cls === 'all' || d.asset_class === filters.cls) && (filters.show === 'all' || !['WAIT', 'ERROR'].includes(d.action)))
      .sort((a, b) => (ORDER[a.action] - ORDER[b.action]) || (Math.abs(b.conviction || 0) - Math.abs(a.conviction || 0)));
    const canShort = (ac) => ac === 'stocks';
    let h = '<thead><tr><th>Symbol</th><th>Decision</th><th>Conviction</th><th>Lean</th><th class="num">Price</th><th>Why</th></tr></thead><tbody>';
    if (!rows.length) h += '<tr><td colspan="6" class="empty">Nothing matches this filter.</td></tr>';
    rows.forEach((d) => {
      const isOpen = open.has(d.symbol);
      const thr = (d.detail && d.detail.threshold) || 0.3;
      h += `<tr class="row" tabindex="0" data-sym="${esc(d.symbol)}" aria-expanded="${isOpen}">
        <td><span class="chev" aria-hidden="true">${isOpen ? '▾' : '▸'}</span><span class="sym">${esc(d.symbol)}</span><span class="cls">${d.asset_class === 'crypto' ? 'crypto' : ''}</span></td>
        <td>${chip(d.action)}</td><td>${meter(d.conviction, thr, canShort(d.asset_class))}</td><td>${leanText(d)}</td>
        <td class="num">${price(d.price)}</td><td>${esc(d.reason)}</td></tr>`;
      if (isOpen) h += `<tr class="detail"><td colspan="6">${detailHtml(d)}</td></tr>`;
    });
    $('#decisions').innerHTML = h + '</tbody>';
    drawFans();
  }

  // ---- what happens next ----
  function renderNext() {
    const items = [];
    const ds = S.decisions || [];
    if (!S.trading_enabled) items.push(['Trading loop is off', 'Start without --no-trading to run hourly cycles.']);
    else if (S.market_open === false) items.push(['US market is closed', `Crypto is still analysed every hour. Stock entries and exits wait for the open${S.next_open ? ' on ' + when(S.next_open) : ''}.`]);
    else items.push(['Every hour', 'All symbols are re-scored; open trades are checked for stop and target every few minutes.']);
    ds.filter((d) => d.action === 'HOLD' && d.detail.trigger).forEach((d) => items.push([`${d.symbol}: holding ${d.detail.position ? d.detail.position.side : ''}`, d.detail.trigger]));
    ds.filter((d) => d.action === 'SKIP').forEach((d) => items.push([`${d.symbol}: candidate, not traded`, d.reason]));
    ds.filter((d) => d.action === 'WAIT' && isNum(d.conviction) && d.detail.threshold)
      .sort((a, b) => Math.abs(b.conviction) / b.detail.threshold - Math.abs(a.conviction) / a.detail.threshold).slice(0, 5)
      .forEach((d) => items.push([`${d.symbol}: closest to a trade (${signed(d.conviction)})`, d.detail.trigger || d.reason]));
    $('#next').innerHTML = '<ul class="next-list">' + items.map(([t, w]) => `<li><div>${esc(t)}</div><div class="what">${esc(w)}</div></li>`).join('') + '</ul>';
  }

  // ---- tables ----
  function renderPositions() {
    const rows = S.positions || [];
    let h = '<thead><tr><th>Symbol</th><th>Side</th><th class="num">Qty</th><th class="num">Entry</th><th class="num">Now</th><th class="num">Unrealised</th><th class="num">Stop</th><th class="num">Target</th><th>Opened</th></tr></thead><tbody>';
    if (!rows.length) h += '<tr><td colspan="9" class="empty">No open positions.</td></tr>';
    rows.forEach((p) => {
      h += `<tr><td class="sym">${esc(p.symbol)}</td><td>${p.side}${p.managed ? '' : ' <span class="k">(not the bot\'s)</span>'}</td><td class="num">${qty(Math.abs(p.qty))}</td>
        <td class="num">${price(p.avg_price)}</td><td class="num">${price(p.price)}</td><td class="num ${tone(p.unrealized)}">${signedUsd(p.unrealized)}</td>
        <td class="num">${price(p.stop)}</td><td class="num">${price(p.target)}</td><td>${when(p.opened_ts)}</td></tr>`;
    });
    $('#positions').innerHTML = h + '</tbody>';
  }

  function renderOrders() {
    const rows = S.orders || [];
    let h = '<thead><tr><th>Time</th><th>Symbol</th><th>Side</th><th class="num">Qty</th><th class="num">Fill</th><th>Status</th><th>Why</th></tr></thead><tbody>';
    if (!rows.length) h += '<tr><td colspan="7" class="empty">No orders yet.</td></tr>';
    rows.forEach((o) => {
      h += `<tr><td>${when(o.ts)}</td><td class="sym">${esc(o.symbol)}</td><td>${esc(o.side)}</td><td class="num">${qty(o.qty)}</td><td class="num">${price(o.price)}</td><td>${esc(o.status)}</td><td>${esc(o.reason)}</td></tr>`;
    });
    $('#orders').innerHTML = h + '</tbody>';
  }

  function renderClosed() {
    const rows = S.closed_trades || [], st = S.stats || {};
    $('#closed-sub').textContent = st.trades ? `${st.trades} closed: ${signedUsd(st.total_pnl)} in total.` : '';
    let h = '<thead><tr><th>Closed</th><th>Symbol</th><th>Side</th><th class="num">Entry</th><th class="num">Exit</th><th class="num">P&amp;L</th><th class="num">R</th><th>Exit</th></tr></thead><tbody>';
    if (!rows.length) h += '<tr><td colspan="8" class="empty">No closed trades yet.</td></tr>';
    rows.forEach((t) => {
      h += `<tr><td>${when(t.closed_ts)}</td><td class="sym">${esc(t.symbol)}</td><td>${esc(t.side)}</td><td class="num">${price(t.entry)}</td><td class="num">${price(t.exit_price)}</td>
        <td class="num ${tone(t.pnl)}">${signedUsd(t.pnl)}</td><td class="num">${signed(t.r_multiple)}</td><td>${esc(t.exit_reason)}</td></tr>`;
    });
    $('#closed').innerHTML = h + '</tbody>';
  }

  // ---- backtest ----
  function renderBacktest() {
    const host = $('#backtest');
    if (!BT) { host.innerHTML = '<div class="empty">No backtest yet. Run <code>python -m quantbot backtest</code> and reload.</div>'; return; }
    const o = BT.overall, spy = BT.benchmarks['SPY buy & hold'];
    const tile = (label, value, delta = '') => `<div class="tile"><div class="label">${label}</div><div class="value">${value}</div><div class="delta">${delta}</div></div>`;
    const group = (title, obj) => {
      let h = `<div><h3>${title}</h3><div class="scroll"><table class="tbl"><thead><tr><th></th><th class="num">Trades</th><th class="num">Win rate</th><th class="num">Avg R</th><th class="num">Net</th></tr></thead><tbody>`;
      Object.entries(obj).forEach(([k, s]) => {
        h += `<tr><td>${esc(k)}</td><td class="num">${s.trades}</td><td class="num">${pct(s.win_rate_pct, 1)}</td><td class="num">${signed(s.avg_r)}</td><td class="num ${tone(s.net_pnl_usd)}">${signedUsd(s.net_pnl_usd, 0)}</td></tr>`;
      });
      return h + '</tbody></table></div></div>';
    };
    const g = BT.gate;
    host.innerHTML = `<p class="sub">Walk-forward test on ${esc(BT.data_source)} hourly bars, ${BT.period.start.slice(0, 10)} to ${BT.period.end.slice(0, 10)}, starting with ${usd(BT.starting_cash, 0)}.
      Weights were fitted only on earlier data; fills at the next bar's open; slippage and crypto fees charged. Generated ${when(BT.generated)}.</p>
      <div class="bt-tiles">
        ${tile('Total return', `<span class="${tone(o.total_return_pct)}">${signedPct(o.total_return_pct)}</span>`, spy ? `SPY buy &amp; hold ${signedPct(spy.total_return_pct)}` : '')}
        ${tile('Sharpe', num(o.sharpe), spy ? `SPY ${num(spy.sharpe)}` : '')}
        ${tile('Max drawdown', pct(o.max_drawdown_pct, 1), spy ? `SPY ${pct(spy.max_drawdown_pct, 1)}` : '')}
        ${tile('Win rate', pct(o.win_rate_pct, 1), `${o.trades} trades`)}
        ${tile('Average trade', signed(o.avg_r) + 'R', `profit factor ${num(o.profit_factor)}`)}
        ${tile('Avg invested', pct(BT.avg_gross_exposure_pct, 0), 'of account')}
      </div>
      <h3>Growth of 100</h3>
      <div id="bt-chart" class="chart"></div>
      <div class="bt-grid">${group('By side', BT.by_side)}${group('By market', BT.by_class)}${group('By exit', BT.by_exit)}</div>
      <p class="note">Of ${g.candidates.toLocaleString()} hourly candidates, the Time Machine blocked ${g.time_machine_blocked.toLocaleString()}, the risk gate ${g.risk_blocked.toLocaleString()}, and ${g.entered.toLocaleString()} were traded.
      Not modelled: short borrow fees, partial fills, halts. One 18-month window is a small sample; past results do not predict future ones.</p>`;
    const cv = BT.curve, names = Object.keys(cv).filter((k) => k !== 'dates' && k !== 'strategy');
    const colors = [css('--s2'), css('--s3')];
    lineChart($('#bt-chart'), {
      title: 'Backtest growth of 100', height: 260, labels: cv.dates,
      series: [{ name: 'QuantBot', color: css('--s1'), values: cv.strategy }].concat(names.map((nm, i) => ({ name: nm, color: colors[i], values: cv[nm] }))),
      fmt: (v) => num(v, 0),
    });
  }

  // ---- loop ----
  function render() {
    renderHeader(); tick();
    if (changed('tiles', [S.account, S.stats, S.positions.length])) renderTiles();
    if (changed('equity', S.equity_curve)) renderEquity();
    if (changed('backdrop', S.cycle && S.cycle.summary)) renderBackdrop();
    if (changed('decisions', [S.decisions, filters, [...open]])) renderDecisions();
    if (changed('next', [S.decisions, S.market_open, S.next_open])) renderNext();
    if (changed('positions', S.positions)) renderPositions();
    if (changed('orders', S.orders)) renderOrders();
    if (changed('closed', [S.closed_trades, S.stats])) renderClosed();
  }

  async function load() {
    try {
      S = await (await fetch('/api/state', { cache: 'no-store' })).json();
      render();
    } catch (e) {
      $('#alerts').innerHTML = '<div class="alert bad">Cannot reach the bot. Is it still running?</div>';
    }
  }
  async function loadBacktest() {
    try { BT = await (await fetch('/api/backtest', { cache: 'no-store' })).json(); } catch (e) { BT = null; }
    renderBacktest();
  }

  function toggle(sym) { open.has(sym) ? open.delete(sym) : open.add(sym); if (S) render(); }
  $('#decisions').addEventListener('click', (e) => { const r = e.target.closest('tr.row'); if (r) toggle(r.dataset.sym); });
  $('#decisions').addEventListener('keydown', (e) => {
    const r = e.target.closest('tr.row');
    if (r && (e.key === 'Enter' || e.key === ' ')) { e.preventDefault(); toggle(r.dataset.sym); }
  });
  [['#f-class', 'cls'], ['#f-show', 'show']].forEach(([sel, key]) => {
    $(sel).addEventListener('click', (e) => {
      const b = e.target.closest('button'); if (!b) return;
      filters[key] = b.dataset.v;
      $(sel).querySelectorAll('button').forEach((x) => x.classList.toggle('on', x === b));
      if (S) render();
    });
  });
  $('#run-now').addEventListener('click', async () => {
    $('#run-now').disabled = true;
    await fetch('/api/run', { method: 'POST', headers: { 'X-Quantbot': 'dashboard' } });
    setTimeout(load, 800);
  });
  window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', () => { Object.keys(seen).forEach((k) => delete seen[k]); if (S) render(); renderBacktest(); });

  load(); loadBacktest();
  setInterval(load, 10000);
  setInterval(tick, 1000);
})();
