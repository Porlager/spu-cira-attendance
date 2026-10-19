'use strict';
// Admin: rooms on the left, sections (optionally filtered by room) on the right.
(() => {
  const {$, el, link, option, json, send, api, run, message} = App;
  let rooms = [], teachers = [], selected = null;

  function fillSelect(select, items, value, empty) {
    select.replaceChildren();
    if (empty) select.append(option('', empty));
    for (const [v, text] of items) select.append(option(v, text));
    if (value !== undefined && value !== null) select.value = String(value);
  }

  function renderRooms() {
    const list = $('#rooms');
    list.replaceChildren();
    for (const r of rooms) {
      const item = el('li');
      item.setAttribute('aria-current', String(selected === r.id));
      const pick = el('button', 'pick');
      pick.type = 'button';
      pick.append(el('span', 'pick-title', r.code), el('span', 'muted small', [r.name, r.sections + ' เซ็ค'].filter(Boolean).join(' · ')));
      pick.addEventListener('click', () => { selected = selected === r.id ? null : r.id; renderRooms(); loadSections(); });
      const remove = el('button', 'btn btn-danger btn-sm', 'ลบ');
      remove.type = 'button';
      remove.addEventListener('click', () => run(async () => {
        if (!confirm('ลบห้อง ' + r.code + '?')) return;
        await api('/rooms/' + r.id, {method: 'DELETE'});
        if (selected === r.id) selected = null;
        await load();
      }));
      item.append(pick, remove);
      list.append(item);
    }
    if (!rooms.length) list.append(el('li', 'empty', 'ยังไม่มีห้อง เพิ่มห้องแรกด้านบน'));
    $('#room-count').textContent = rooms.length ? rooms.length + ' ห้อง' : '';
    fillSelect($('#section-room'), rooms.map(r => [r.id, r.code + (r.name ? ' · ' + r.name : '')]), selected, rooms.length ? null : 'เพิ่มห้องก่อน');
    const room = rooms.find(r => r.id === selected);
    $('#room-title').textContent = room ? 'ห้อง ' + room.code : 'ทุกห้อง';
    $('#show-all').hidden = !room;
  }

  async function loadSections() {
    const {sections} = await json('/sections' + (selected ? '?room=' + selected : ''));
    const body = $('#sections');
    body.replaceChildren();
    for (const s of sections) {
      const tr = el('tr');
      const course = el('td');
      course.append(el('div', null, s.course_code), el('div', 'muted small', s.course_name || ''));
      const faces = el('td', 'num', s.with_faces + '/' + s.students);
      if (s.students && s.with_faces < s.students) faces.classList.add('warn-text');
      const action = el('td', 'actions');
      action.append(link('/admin/sections/' + s.id, 'จัดการรายชื่อ', 'btn btn-secondary btn-sm'));
      tr.append(course, el('td', null, s.section), el('td', null, s.room_code),
        el('td', s.teacher_name ? null : 'muted', s.teacher_name || 'ยังไม่กำหนด'), el('td', 'num', s.students), faces, action);
      body.append(tr);
    }
    const empty = $('#sections-empty');
    empty.hidden = sections.length > 0;
    empty.textContent = rooms.length ? 'ยังไม่มีเซ็ค' + (selected ? 'ในห้องนี้' : '') + ' เพิ่มได้จากฟอร์มด้านบน' : 'เพิ่มห้องก่อน แล้วจึงเพิ่มเซ็ค';
  }

  async function load() {
    const [roomData, userData] = await Promise.all([json('/rooms'), json('/users')]);
    rooms = roomData.rooms;
    teachers = userData.users;
    renderRooms();
    fillSelect($('#section-teacher'), teachers.map(u => [u.id, u.name + (u.role === 'admin' ? ' (ผู้ดูแล)' : '')]), null, 'ยังไม่กำหนด');
    await loadSections();
  }

  $('#room-form').addEventListener('submit', event => {
    event.preventDefault();
    run(async () => {
      const {id} = await send('/rooms', 'POST', {code: $('#room-code').value, name: $('#room-name').value});
      $('#room-form').reset();
      selected = id;
      message('เพิ่มห้องแล้ว');
      await load();
    });
  });
  $('#section-form').addEventListener('submit', event => {
    event.preventDefault();
    run(async () => {
      const teacher = $('#section-teacher').value;
      const {id} = await send('/sections', 'POST', {
        course_code: $('#course-code').value, section: $('#section-no').value, course_name: $('#course-name').value,
        room_id: Number($('#section-room').value) || null, teacher_id: teacher ? Number(teacher) : null,
      });
      location.href = '/admin/sections/' + id + '?new=1';
    });
  });
  $('#show-all').addEventListener('click', () => { selected = null; renderRooms(); run(loadSections); });
  run(load);
})();
