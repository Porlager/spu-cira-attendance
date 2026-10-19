'use strict';
// Shared helpers for every page: API calls with the local key, formatting, top bar status.
const App = (() => {
  const key = document.querySelector('meta[name="api-key"]').content;
  const $ = (selector, root = document) => root.querySelector(selector);
  const BEHAVIOR = {
    attentive: 'ตั้งใจเรียน', inattentive: 'ไม่ตั้งใจ', sleeping: 'หลับ', phone: 'ใช้โทรศัพท์',
    person: 'คน (ไม่ระบุพฤติกรรม)', ignore: 'ไม่นับ', unknown: 'ยังไม่ระบุ', not_seen: 'ไม่เห็นในภาพ',
  };
  const SOURCE = {
    'local-face': 'กล้องเครื่องนี้', 'cira-face': 'CiRA', cira: 'CiRA', 'cira-person': 'CiRA (นับคน)', simulator: 'ข้อมูลจำลอง',
  };
  const CIRA = {
    live: 'CiRA กำลังส่งข้อมูล', connected: 'CiRA เชื่อมต่ออยู่ · ไม่มีเฟรมใหม่',
    error: 'CiRA ส่งมาแต่ถูกปฏิเสธ', offline: 'ยังไม่ได้รับข้อมูลจาก CiRA',
  };

  async function api(path, options = {}) {
    const headers = {'X-API-Key': key, ...(options.body ? {'Content-Type': 'application/json'} : {}), ...options.headers};
    const response = await fetch('/api' + path, {...options, headers});
    if (!response.ok) {
      let text = 'คำขอล้มเหลว (' + response.status + ')';
      try { text = (await response.json()).error || text; } catch (_) { /* not JSON */ }
      throw new Error(text);
    }
    return response;
  }
  const json = async (path, options) => (await api(path, options)).json();
  const send = (path, method, data) => json(path, {method, body: JSON.stringify(data || {})});

  function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = text;
    return node;
  }
  function chip(behavior, label) {
    const node = el('span', 'chip', label || BEHAVIOR[behavior] || behavior);
    node.dataset.b = behavior;
    return node;
  }
  const time = iso => iso ? new Date(iso).toLocaleTimeString('th-TH', {hour: '2-digit', minute: '2-digit'}) : '—';
  const dateTime = iso => iso ? new Date(iso).toLocaleString('th-TH', {day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit'}) : '—';
  const pct = value => value === null || value === undefined ? '—' : Math.round(value) + '%';
  const number = value => value === null || value === undefined ? '—' : String(Math.round(value * 10) / 10);
  function duration(seconds) {
    if (seconds < 60) return seconds + ' วิ';
    const minutes = Math.floor(seconds / 60);
    return minutes < 60 ? minutes + ' นาที' : Math.floor(minutes / 60) + ' ชม. ' + (minutes % 60) + ' นาที';
  }
  function ago(seconds) {
    if (seconds < 2) return 'เมื่อสักครู่';
    if (seconds < 60) return Math.round(seconds) + ' วินาทีที่แล้ว';
    if (seconds < 3600) return Math.round(seconds / 60) + ' นาทีที่แล้ว';
    return Math.round(seconds / 3600) + ' ชั่วโมงที่แล้ว';
  }

  let messageTimer;
  function message(text, error = false) {
    const box = $('#message');
    box.textContent = text;
    box.classList.toggle('error', error);
    box.hidden = false;
    clearTimeout(messageTimer);
    messageTimer = setTimeout(() => { box.hidden = true; }, error ? 8000 : 4000);
  }
  async function run(fn) {
    try { await fn(); } catch (error) { message(error.message, true); }
  }

  let session = null;
  function renderStatus(data) {
    session = data.session;
    $('#session-title').textContent = session ? session.title : 'ยังไม่เปิดคาบเรียน';
    $('#session-meta').textContent = session ? '#' + session.id + ' · เริ่ม ' + time(session.started_at) : '';
    $('#sim-badge').hidden = !(session && session.simulated);
    $('#close-session').hidden = !session;
    const cira = data.cira;
    const status = $('#cira-status');
    let text = CIRA[cira.status];
    if (cira.status === 'live') text += ' · ' + ago(cira.seconds_since_frame);
    if (cira.status === 'error' && cira.last_error) text += ': ' + cira.last_error;
    if (cira.status === 'offline' && cira.seconds_since_contact !== null) text = 'CiRA เงียบไป · ติดต่อล่าสุด ' + ago(cira.seconds_since_contact);
    status.dataset.status = cira.status;
    status.title = cira.last_error || '';
    $('#cira-text').textContent = text;
  }
  const refreshStatus = async () => renderStatus(await json('/status'));

  async function download(path, filename) {
    const blob = await (await api(path)).blob();
    const url = URL.createObjectURL(blob);
    const link = el('a');
    link.href = url;
    link.download = filename;
    link.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }

  const app = {
    key, $, api, json, send, el, chip, time, dateTime, pct, number, duration, ago, message, run,
    renderStatus, refreshStatus, download, BEHAVIOR, SOURCE,
    get session() { return session; },
    onSessionChange: null,
  };

  $('#close-session').addEventListener('click', () => run(async () => {
    if (!session || !confirm('ปิดคาบ "' + session.title + '"?\nหลังปิดแล้ว CiRA จะส่งข้อมูลเข้าคาบนี้ไม่ได้อีก')) return;
    await send('/sessions/' + session.id + '/close', 'POST');
    message('ปิดคาบเรียนแล้ว');
    await refreshStatus();
    if (app.onSessionChange) app.onSessionChange();
  }));

  // The live page refreshes status from its own 1-second loop.
  if (document.body.dataset.page !== 'live') {
    run(refreshStatus);
    setInterval(() => { if (!document.hidden) refreshStatus().catch(() => {}); }, 5000);
  }
  return app;
})();
