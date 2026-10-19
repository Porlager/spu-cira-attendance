'use strict';
// Shared helpers for every page: API calls with the CSRF token, formatting, top bar status.
const App = (() => {
  const csrf = document.querySelector('meta[name="csrf-token"]').content;
  const $ = (selector, root = document) => root.querySelector(selector);
  const page = document.body.dataset.page;
  const role = document.body.dataset.role;
  const uid = Number(document.body.dataset.uid) || null;
  const params = new URLSearchParams(location.search);
  const sessionId = /^\d+$/.test(params.get('session') || '') ? Number(params.get('session')) : null;
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
    const headers = {'X-CSRF-Token': csrf, ...(options.body ? {'Content-Type': 'application/json'} : {}), ...options.headers};
    const response = await fetch('/api' + path, {...options, headers, credentials: 'same-origin'});
    if (response.status === 401) {
      location.href = '/login?next=' + encodeURIComponent(location.pathname + location.search);
      throw new Error('ต้องเข้าสู่ระบบใหม่');
    }
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
  function link(href, text, className) {
    const node = el('a', className, text);
    node.href = href;
    return node;
  }
  function option(value, text) {
    const node = el('option', null, text);
    node.value = value;
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
  const sectionLabel = s => s.course_code ? s.course_code + ' · sec ' + (s.section_name || s.section) : s.title;

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
    const s = session;
    $('#session-title').textContent = s ? s.title + (s.room_code ? ' · ห้อง ' + s.room_code : '') : 'ยังไม่เปิดคาบเรียน';
    let meta = s ? '#' + s.id + ' · เริ่ม ' + time(s.started_at) + (s.teacher_name ? ' · ' + s.teacher_name : '') : '';
    if (s && s.ended_at) meta += ' · ปิดแล้ว';
    if (data.open_sessions > 1) meta += (meta ? ' · ' : '') + 'เปิดอยู่ ' + data.open_sessions + ' ห้อง';
    $('#session-meta').textContent = meta;
    $('#sim-badge').hidden = !(s && s.simulated);
    $('#close-session').hidden = !(s && !s.ended_at && page === 'live');
    const open = $('#open-live');
    open.hidden = !(s && !s.ended_at && page !== 'live');
    if (s) open.href = '/live?session=' + s.id;
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
  const refreshStatus = async () => renderStatus(await json('/status' + (sessionId ? '?session=' + sessionId : '')));

  async function download(path, filename) {
    const blob = await (await api(path)).blob();
    const url = URL.createObjectURL(blob);
    const a = el('a');
    a.href = url;
    a.download = filename;
    a.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }

  function readFileBase64(file) {
    return new Promise((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = () => resolve(String(reader.result).split(',').pop());
      reader.onerror = () => reject(new Error('อ่านไฟล์ไม่ได้'));
      reader.readAsDataURL(file);
    });
  }

  const app = {
    $, api, json, send, el, link, option, chip, time, dateTime, pct, number, duration, ago, message, run,
    renderStatus, refreshStatus, download, readFileBase64, sectionLabel, BEHAVIOR, SOURCE, page, role, uid, sessionId,
    get session() { return session; },
    onSessionChange: null,
  };

  $('#close-session').addEventListener('click', () => run(async () => {
    if (!session || !confirm('ปิดคาบ "' + session.title + '"?\nหลังปิดแล้ว CiRA จะส่งข้อมูลเข้าคาบนี้ไม่ได้อีก')) return;
    await send('/sessions/' + session.id + '/close', 'POST');
    location.href = '/reports?session=' + session.id;
  }));

  // The live page refreshes status from its own 1-second loop.
  if (page !== 'live') {
    run(refreshStatus);
    setInterval(() => { if (!document.hidden) refreshStatus().catch(() => {}); }, 5000);
  }
  return app;
})();
