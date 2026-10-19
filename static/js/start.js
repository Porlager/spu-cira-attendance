'use strict';
// Teacher flow: pick a room, then one of my sections in it, then start the class.
(() => {
  const {$, el, json, send, run, message, time, sectionLabel} = App;
  let rooms = [], room = null;

  function renderMine(sessions) {
    const mine = sessions.find(s => !s.ended_at);
    $('#my-open').hidden = !mine;
    if (!mine) return;
    $('#my-open-title').textContent = 'คุณมีคาบที่เปิดอยู่: ' + sectionLabel(mine) + (mine.room_code ? ' · ห้อง ' + mine.room_code : '');
    $('#my-open-meta').textContent = 'เริ่ม ' + time(mine.started_at) + ' · ปิดคาบได้จากหน้าห้องเรียน';
    $('#my-open-link').href = '/live?session=' + mine.id;
  }

  function renderRooms() {
    const grid = $('#rooms');
    grid.replaceChildren();
    for (const r of rooms) {
      const card = el('button', 'room-card');
      card.type = 'button';
      const busy = r.open_session;
      card.append(el('span', 'room-code', r.code), el('span', 'room-name', r.name || ' '),
        el('span', 'room-meta', r.sections + ' เซ็ค' + (App.role === 'teacher' ? 'ที่คุณสอน' : '')));
      const state = el('span', 'room-state', busy ? 'มีคาบ: ' + busy.title + (busy.teacher_name ? ' · ' + busy.teacher_name : '') : 'ว่าง');
      state.dataset.busy = busy ? (busy.mine ? 'mine' : 'other') : 'free';
      card.append(state);
      card.addEventListener('click', () => pickRoom(r.id));
      grid.append(card);
    }
    const empty = $('#rooms-empty');
    empty.hidden = rooms.length > 0;
    if (!rooms.length) {
      const admin = App.role !== 'teacher';
      $('#rooms-empty-title').textContent = admin ? 'ยังไม่มีห้อง' : 'ยังไม่มีเซ็คที่คุณสอน';
      $('#rooms-empty-text').replaceChildren(admin
        ? App.link('/admin/sections', 'สร้างห้องและเซ็คก่อน')
        : 'ติดต่อผู้ดูแลระบบให้กำหนดเซ็คที่คุณสอน');
    }
  }

  async function pickRoom(id) {
    room = rooms.find(r => r.id === id);
    if (!room) return showRooms();
    history.replaceState(null, '', '/?room=' + id);
    $('#rooms-step').hidden = true;
    $('#sections-step').hidden = false;
    $('#room-label').textContent = room.code + (room.name ? ' · ' + room.name : '');
    const busy = room.open_session;
    $('#room-busy').hidden = !(busy && !busy.mine);
    if (busy && !busy.mine) $('#room-busy').textContent = 'ห้องนี้มีคาบเปิดอยู่ (' + busy.title + ') เริ่มคาบใหม่ได้เมื่อคาบนั้นปิดแล้ว';
    const {sections} = await json('/sections?room=' + id);
    const list = $('#sections');
    list.replaceChildren();
    for (const s of sections) {
      const item = el('li', 'section-item');
      const info = el('div', 'section-info');
      info.append(el('div', 'section-name', s.course_code + ' · sec ' + s.section),
        el('div', 'muted small', [s.course_name, s.teacher_name, 'นักศึกษา ' + s.students + ' คน · มีใบหน้า ' + s.with_faces].filter(Boolean).join(' · ')));
      const action = el('div', 'section-action');
      if (s.open_session) {
        action.append(App.link('/live?session=' + s.open_session.id, 'กลับเข้าห้องเรียน', 'btn btn-primary btn-sm'));
      } else {
        const start = el('button', 'btn btn-primary btn-sm', 'เริ่มคาบ');
        start.type = 'button';
        start.disabled = !!busy;
        if (!s.students) start.title = 'เซ็คนี้ยังไม่มีรายชื่อนักศึกษา';
        start.addEventListener('click', () => run(async () => {
          start.disabled = true;
          try {
            const {id: sid} = await send('/sessions', 'POST', {section_id: s.id});
            location.href = '/live?session=' + sid;
          } finally {
            start.disabled = false;
          }
        }));
        action.append(start);
      }
      if (s.students && s.with_faces < s.students) {
        action.prepend(el('span', 'muted small', 'ยังไม่มีใบหน้า ' + (s.students - s.with_faces) + ' คน'));
      }
      item.append(info, action);
      list.append(item);
    }
    $('#sections-empty').hidden = sections.length > 0;
  }

  function showRooms() {
    room = null;
    history.replaceState(null, '', '/');
    $('#rooms-step').hidden = false;
    $('#sections-step').hidden = true;
  }

  async function load() {
    const [roomData, sessionData] = await Promise.all([json('/rooms'), json('/sessions')]);
    rooms = roomData.rooms;
    renderMine(sessionData.sessions.filter(s => s.teacher_id === App.uid));
    renderRooms();
    const wanted = Number(new URLSearchParams(location.search).get('room'));
    if (wanted && rooms.some(r => r.id === wanted)) await pickRoom(wanted);
  }

  $('#change-room').addEventListener('click', showRooms);
  run(load);
  setInterval(() => {
    if (document.hidden) return;
    json('/rooms').then(data => { rooms = data.rooms; if (!room) renderRooms(); }).catch(() => {});
  }, 10000);
  if (new URLSearchParams(location.search).get('closed')) message('ปิดคาบเรียนแล้ว');
})();
