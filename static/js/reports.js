'use strict';
(() => {
  const {$, el, json, run, pct, number, time, dateTime, download, SOURCE, BEHAVIOR} = App;
  const NS = 'http://www.w3.org/2000/svg';
  const W = 560, H = 200, PAD = {left: 36, right: 12, top: 12, bottom: 26};
  let sessions = [], report = null;

  function svg(tag, attrs, text) {
    const node = document.createElementNS(NS, tag);
    for (const [k, v] of Object.entries(attrs || {})) node.setAttribute(k, v);
    if (text !== undefined) node.textContent = text;
    return node;
  }
  const minuteLabel = minute => new Date(minute + ':00Z').toLocaleTimeString('th-TH', {hour: '2-digit', minute: '2-digit'});

  // Shared frame: horizontal gridlines with y labels, a few x labels. Returns x/y scale functions.
  function frame(root, rows, max, unit) {
    root.replaceChildren();
    root.setAttribute('viewBox', '0 0 ' + W + ' ' + H);
    const innerW = W - PAD.left - PAD.right, innerH = H - PAD.top - PAD.bottom;
    const x = i => PAD.left + (rows.length === 1 ? innerW / 2 : i * innerW / (rows.length - 1));
    const y = v => PAD.top + innerH - (v / max) * innerH;
    for (const v of [0, max / 2, max]) {
      root.append(svg('line', {class: 'grid', x1: PAD.left, x2: W - PAD.right, y1: y(v), y2: y(v)}));
      root.append(svg('text', {class: 'axis', x: PAD.left - 6, y: y(v) + 4, 'text-anchor': 'end'}, number(v) + unit));
    }
    const picks = [...new Set([0, Math.floor((rows.length - 1) / 2), rows.length - 1])];
    for (const i of picks) {
      const anchor = rows.length === 1 ? 'middle' : i === 0 ? 'start' : i === rows.length - 1 ? 'end' : 'middle';
      root.append(svg('text', {class: 'axis', x: x(i), y: H - 6, 'text-anchor': anchor}, minuteLabel(rows[i].minute)));
    }
    return {x, y, innerW};
  }

  function empty(root) {
    root.replaceChildren();
    root.setAttribute('viewBox', '0 0 ' + W + ' ' + H);
    root.append(svg('text', {class: 'empty-text', x: W / 2, y: H / 2, 'text-anchor': 'middle'}, 'ยังไม่มีข้อมูลพฤติกรรมในคาบนี้'));
  }

  function lineChart(root, rows) {
    if (!rows.length) return empty(root);
    const {x, y} = frame(root, rows, 100, '%');
    let d = '', pen = false;
    rows.forEach((r, i) => {
      if (r.attentive_pct === null) { pen = false; return; }
      d += (pen ? 'L' : 'M') + x(i).toFixed(1) + ' ' + y(r.attentive_pct).toFixed(1);
      pen = true;
    });
    root.append(svg('path', {class: 'line', d}));
    rows.forEach((r, i) => {
      if (r.attentive_pct === null) return;
      const point = svg('circle', {class: 'point', cx: x(i), cy: y(r.attentive_pct), r: rows.length > 40 ? 0 : 3});
      point.append(svg('title', {}, minuteLabel(r.minute) + ' · ตั้งใจ ' + pct(r.attentive_pct) + ' · ' + r.frames + ' เฟรม'));
      root.append(point);
    });
  }

  function barChart(root, rows) {
    if (!rows.length) return empty(root);
    const max = Math.max(1, Math.ceil(Math.max(...rows.map(r => r.sleeping_avg))));
    const {x, y, innerW} = frame(root, rows, max, '');
    const bw = Math.max(2, Math.min(18, innerW / rows.length * 0.6));
    rows.forEach((r, i) => {
      const top = y(r.sleeping_avg), base = y(0);
      const bar = svg('rect', {class: 'bar', x: x(i) - bw / 2, y: top, width: bw, height: Math.max(0, base - top), rx: 1});
      bar.append(svg('title', {}, minuteLabel(r.minute) + ' · หลับเฉลี่ย ' + number(r.sleeping_avg) + ' คน'));
      root.append(bar);
    });
  }

  function shareBar(shares, behaviors) {
    const bar = el('div', 'sharebar');
    for (const b of behaviors) {
      if (!shares[b]) continue;
      const part = el('span');
      part.dataset.b = b;
      part.style.width = (shares[b] * 100) + '%';
      part.title = BEHAVIOR[b] + ' ' + pct(shares[b] * 100);
      bar.append(part);
    }
    return bar;
  }

  function render() {
    const {session, summary, timeline, students} = report;
    const behaviors = ['attentive', 'inattentive', 'sleeping'].concat(report.phone_enabled ? ['phone'] : []);
    $('#r-present').textContent = summary.present;
    $('#r-registered').textContent = '/' + summary.registered;
    $('#r-time').textContent = 'เริ่ม ' + time(session.started_at) + (session.ended_at ? ' · ปิด ' + time(session.ended_at) : ' · ยังเปิดอยู่');
    $('#r-attentive').textContent = pct(summary.avg_attentive_pct);
    $('#r-sleeping').textContent = summary.max_sleeping === null ? '—' : summary.max_sleeping;
    $('#r-frames').textContent = summary.frames;
    $('#r-sources').textContent = summary.sources.length ? 'จาก ' + summary.sources.map(s => SOURCE[s] || s).join(', ') : 'ยังไม่มีข้อมูลจาก CiRA';
    lineChart($('#chart-attentive'), timeline);
    barChart($('#chart-sleeping'), timeline);

    const legend = $('#share-legend');
    legend.replaceChildren();
    for (const b of behaviors) {
      const item = el('span');
      item.dataset.b = b;
      item.append(el('i'), BEHAVIOR[b]);
      legend.append(item);
    }
    const body = $('#report-students');
    body.replaceChildren();
    for (const s of students) {
      const tr = el('tr');
      const share = el('td');
      share.append(s.frames ? shareBar(s.shares, behaviors) : el('span', 'muted', 'ไม่มีข้อมูล'));
      tr.append(el('td', 'id', s.id), el('td', null, s.name),
        s.present ? el('td', null, time(s.seen_at)) : el('td', 'muted', 'ไม่พบ'),
        el('td', 'muted', s.source ? SOURCE[s.source] || s.source : '—'),
        el('td', 'num', s.frames), share,
        el('td', 'num', s.frames ? pct(s.shares.attentive * 100) : '—'),
        el('td', 'num', s.frames ? pct(s.shares.sleeping * 100) : '—'));
      body.append(tr);
    }
  }

  async function loadReport() {
    const id = $('#session-select').value;
    if (!id) return;
    report = await json('/sessions/' + id + '/report');
    render();
  }

  async function loadSessions() {
    sessions = (await json('/sessions')).sessions;
    const select = $('#session-select');
    const previous = select.value || new URLSearchParams(location.search).get('session');
    select.replaceChildren();
    for (const s of sessions) {
      const option = el('option', null, '#' + s.id + ' ' + s.title + ' · ' + dateTime(s.started_at) + (s.simulated ? ' (ข้อมูลจำลอง)' : ''));
      option.value = s.id;
      select.append(option);
    }
    if (previous && sessions.some(s => String(s.id) === String(previous))) select.value = previous;
    $('#report-empty').hidden = sessions.length > 0;
    $('#report').hidden = !sessions.length;
    $('#export').disabled = !sessions.length;
    await loadReport();
  }

  $('#session-select').addEventListener('change', () => {
    history.replaceState(null, '', '?session=' + $('#session-select').value);
    run(loadReport);
  });
  $('#export').addEventListener('click', () => run(async () => {
    const s = sessions.find(item => String(item.id) === $('#session-select').value);
    if (s) await download('/sessions/' + s.id + '/export', 'attendance-' + s.id + (s.simulated ? '-simulated' : '') + '.csv');
  }));
  App.onSessionChange = () => run(loadSessions);
  run(loadSessions);
  // Keep an open session's report fresh.
  setInterval(() => {
    const s = sessions.find(item => String(item.id) === $('#session-select').value);
    if (s && !s.ended_at && !document.hidden) loadReport().catch(() => {});
  }, 10000);
})();
