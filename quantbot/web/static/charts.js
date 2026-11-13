/* Small SVG line/fan chart: hairline grid, 2px lines, ringed end-dots,
   crosshair tooltip, legend for 2+ series, and a data-table twin. */
(function () {
  const NS = 'http://www.w3.org/2000/svg';
  const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

  function node(name, attrs, parent) {
    const n = document.createElementNS(NS, name);
    for (const k in attrs) n.setAttribute(k, attrs[k]);
    if (parent) parent.appendChild(n);
    return n;
  }

  function niceTicks(lo, hi, count) {
    if (!(hi > lo)) { hi = lo + 1; }
    const raw = (hi - lo) / count;
    const mag = Math.pow(10, Math.floor(Math.log10(raw)));
    const step = [1, 2, 2.5, 5, 10].map((m) => m * mag).find((s) => s >= raw);
    const out = [];
    for (let v = Math.ceil(lo / step) * step; v <= hi + step * 1e-9; v += step) out.push(+v.toFixed(10));
    return out;
  }

  const tip = () => document.getElementById('tip');
  function showTip(html, x, y) {
    const t = tip();
    t.innerHTML = html;
    t.hidden = false;
    const w = t.offsetWidth, h = t.offsetHeight;
    t.style.left = Math.min(x + 14, window.innerWidth - w - 8) + 'px';
    t.style.top = Math.min(y + 14, window.innerHeight - h - 8) + 'px';
  }
  function hideTip() { tip().hidden = true; }

  const observed = new WeakSet();
  const ro = new ResizeObserver((entries) => {
    for (const e of entries) {
      const host = e.target;
      if (host._opt && Math.abs((host._w || 0) - e.contentRect.width) > 4) draw(host, host._opt);
    }
  });

  function lineChart(host, opt) {
    host._opt = opt;
    if (!observed.has(host)) { observed.add(host); ro.observe(host); }
    draw(host, opt);
  }

  function draw(host, opt) {
    const series = opt.series || [], bands = opt.bands || [], hlines = opt.hlines || [];
    const labels = opt.labels || [];
    const n = labels.length;
    const fmt = opt.fmt || ((v) => String(v));
    host.innerHTML = '';
    host._w = host.clientWidth;
    if (n < 2) {
      host.innerHTML = '<div class="empty">' + esc(opt.empty || 'Not enough data yet.') + '</div>';
      return;
    }
    const W = Math.max(host.clientWidth, 280), H = opt.height || 220;
    const multi = series.length >= 2;
    const m = { l: 58, r: (multi || hlines.length) ? 104 : 16, t: 10, b: 24 };
    const pw = W - m.l - m.r, ph = H - m.t - m.b;

    let lo = Infinity, hi = -Infinity;
    const scan = (arr) => arr.forEach((v) => { if (v != null && isFinite(v)) { lo = Math.min(lo, v); hi = Math.max(hi, v); } });
    series.forEach((s) => scan(s.values));
    bands.forEach((b) => { scan(b.lo); scan(b.hi); });
    hlines.forEach((h) => scan([h.value]));
    const pad = (hi - lo || Math.abs(hi) || 1) * 0.06;
    lo -= pad; hi += pad;
    const X = (i) => m.l + (i / (n - 1)) * pw;
    const Y = (v) => m.t + (1 - (v - lo) / (hi - lo)) * ph;

    if (multi || opt.legend) {
      const lg = document.createElement('div');
      lg.className = 'legend';
      lg.innerHTML = series.map((s) => `<span><i class="line" style="background:${s.color}"></i>${esc(s.name)}</span>`).join('') + (opt.legend || '');
      host.appendChild(lg);
    }

    const svg = node('svg', { viewBox: `0 0 ${W} ${H}`, height: H, role: 'img', 'aria-label': opt.title || 'chart' });
    host.appendChild(svg);
    const grid = css('--grid'), axis = css('--axis'), surface = css('--surface');

    niceTicks(lo, hi, 4).forEach((v) => {
      node('line', { x1: m.l, x2: m.l + pw, y1: Y(v), y2: Y(v), stroke: grid, 'stroke-width': 1 }, svg);
      node('text', { x: m.l - 8, y: Y(v) + 4, 'text-anchor': 'end' }, svg).textContent = fmt(v);
    });
    node('line', { x1: m.l, x2: m.l + pw, y1: m.t + ph, y2: m.t + ph, stroke: axis, 'stroke-width': 1 }, svg);
    const xt = opt.xTicks || [0, Math.floor((n - 1) / 2), n - 1];
    xt.forEach((i, k) => {
      const anchor = k === 0 ? 'start' : k === xt.length - 1 ? 'end' : 'middle';
      node('text', { x: X(i), y: H - 6, 'text-anchor': anchor }, svg).textContent = opt.xFmt ? opt.xFmt(labels[i], i) : labels[i];
    });

    bands.forEach((b) => {
      let d = '';
      for (let i = 0; i < n; i++) d += (i ? 'L' : 'M') + X(i) + ' ' + Y(b.hi[i]);
      for (let i = n - 1; i >= 0; i--) d += 'L' + X(i) + ' ' + Y(b.lo[i]);
      node('path', { d: d + 'Z', fill: b.color, opacity: b.opacity }, svg);
    });

    hlines.forEach((h) => {
      node('line', { x1: m.l, x2: m.l + pw, y1: Y(h.value), y2: Y(h.value), stroke: h.color, 'stroke-width': 1.5 }, svg);
      node('text', { x: m.l + pw + 8, y: Y(h.value) + 4, class: 'lbl' }, svg).textContent = `${h.label} ${fmt(h.value)}`;
    });

    const ends = [];
    series.forEach((s) => {
      let d = '', started = false, last = -1;
      s.values.forEach((v, i) => {
        if (v == null || !isFinite(v)) return;
        d += (started ? 'L' : 'M') + X(i) + ' ' + Y(v);
        started = true; last = i;
      });
      node('path', { d, fill: 'none', stroke: s.color, 'stroke-width': 2, 'stroke-linejoin': 'round', 'stroke-linecap': 'round' }, svg);
      if (last >= 0) {
        node('circle', { cx: X(last), cy: Y(s.values[last]), r: 4, fill: s.color, stroke: surface, 'stroke-width': 2 }, svg);
        ends.push({ y: Y(s.values[last]), text: `${s.name} ${fmt(s.values[last])}`, x: X(last) });
      }
    });
    // direct end-labels only when they do not collide; the legend carries identity otherwise
    if (multi && series.length <= 4 && !hlines.length) {
      const sorted = ends.slice().sort((a, b) => a.y - b.y);
      const clear = sorted.every((e, i) => i === 0 || e.y - sorted[i - 1].y >= 13);
      if (clear) ends.forEach((e) => { node('text', { x: e.x + 9, y: e.y + 4, class: 'lbl' }, svg).textContent = e.text; });
    }

    // hover layer: crosshair + tooltip on the nearest x
    const cross = node('line', { y1: m.t, y2: m.t + ph, stroke: axis, 'stroke-width': 1, visibility: 'hidden' }, svg);
    const dots = series.map((s) => node('circle', { r: 4, fill: s.color, stroke: surface, 'stroke-width': 2, visibility: 'hidden' }, svg));
    const hit = node('rect', { x: m.l, y: m.t, width: pw, height: ph, fill: 'transparent' }, svg);
    hit.addEventListener('mousemove', (ev) => {
      const box = svg.getBoundingClientRect();
      const px = (ev.clientX - box.left) * (W / box.width);
      const i = Math.max(0, Math.min(n - 1, Math.round(((px - m.l) / pw) * (n - 1))));
      cross.setAttribute('x1', X(i)); cross.setAttribute('x2', X(i)); cross.setAttribute('visibility', 'visible');
      let rows = '';
      series.forEach((s, k) => {
        const v = s.values[i];
        if (v == null || !isFinite(v)) { dots[k].setAttribute('visibility', 'hidden'); return; }
        dots[k].setAttribute('cx', X(i)); dots[k].setAttribute('cy', Y(v)); dots[k].setAttribute('visibility', 'visible');
        rows += `<div class="r"><span><i style="background:${s.color}"></i>${esc(s.name)}</span><span>${esc(fmt(v))}</span></div>`;
      });
      bands.forEach((b) => {
        if (b.name) rows += `<div class="r"><span>${esc(b.name)}</span><span>${esc(fmt(b.lo[i]))} – ${esc(fmt(b.hi[i]))}</span></div>`;
      });
      const head = opt.tipFmt ? opt.tipFmt(labels[i], i) : labels[i];
      showTip(`<div class="t">${esc(head)}</div>${rows}`, ev.clientX, ev.clientY);
    });
    hit.addEventListener('mouseleave', () => {
      cross.setAttribute('visibility', 'hidden');
      dots.forEach((d) => d.setAttribute('visibility', 'hidden'));
      hideTip();
    });

    if (opt.table !== false) {
      const det = document.createElement('details');
      det.className = 'data';
      const cols = series.map((s) => `<th class="num">${esc(s.name)}</th>`).join('') +
        bands.filter((b) => b.name).map((b) => `<th class="num">${esc(b.name)}</th>`).join('');
      let body = '';
      for (let i = n - 1; i >= 0; i--) {
        body += `<tr><td>${esc(opt.tipFmt ? opt.tipFmt(labels[i], i) : labels[i])}</td>` +
          series.map((s) => `<td class="num">${s.values[i] == null ? '' : esc(fmt(s.values[i]))}</td>`).join('') +
          bands.filter((b) => b.name).map((b) => `<td class="num">${esc(fmt(b.lo[i]))} – ${esc(fmt(b.hi[i]))}</td>`).join('') + '</tr>';
      }
      det.innerHTML = `<summary>Show as table</summary><div class="scroll"><table class="tbl"><thead><tr><th>${esc(opt.xName || 'Date')}</th>${cols}</tr></thead><tbody>${body}</tbody></table></div>`;
      host.appendChild(det);
    }
  }

  window.Charts = { lineChart, showTip, hideTip, esc, css };
})();
