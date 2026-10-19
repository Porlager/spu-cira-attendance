'use strict';
// Admin: one section's roster, CSV import with preview, quick add, and section settings.
(() => {
  const {$, el, link, option, json, send, api, run, message, readFileBase64} = App;
  const id = Number(location.pathname.split('/').pop());
  let section = null, students = [], pending = null;

  function render() {
    $('#section-title').textContent = section.course_code + ' · sec ' + section.section;
    document.title = section.course_code + ' sec ' + section.section + ' · SPU Classroom';
    const missing = students.filter(s => !s.samples).length;
    $('#s-students').textContent = students.length;
    $('#s-room').textContent = 'ห้อง ' + section.room_code + (section.course_name ? ' · ' + section.course_name : '');
    $('#s-faces').textContent = students.length - missing;
    $('#s-missing').textContent = missing;
    $('#s-teacher').textContent = section.teacher_name || 'ยังไม่กำหนด';
    renderRoster();
  }

  function renderRoster() {
    const query = $('#roster-search').value.trim().toLowerCase();
    const missingOnly = $('#missing-only').checked;
    const rows = students.filter(s => (!missingOnly || !s.samples) && (!query || s.id.toLowerCase().includes(query) || s.name.toLowerCase().includes(query)));
    const body = $('#roster');
    body.replaceChildren();
    for (const s of rows) {
      const tr = el('tr');
      const face = el('td');
      if (s.samples) face.append(App.chip('attentive', 'มี ' + s.samples + ' ภาพ'));
      else face.append(link('/enroll?student=' + encodeURIComponent(s.id), 'ยังไม่มี · ถ่ายภาพ', 'chip-link'));
      const action = el('td', 'actions');
      const remove = el('button', 'btn btn-danger btn-sm', 'เอาออก');
      remove.type = 'button';
      remove.addEventListener('click', () => run(async () => {
        if (!confirm('เอา ' + s.id + ' ' + s.name + ' ออกจากเซ็คนี้?\nข้อมูลนักศึกษาและภาพใบหน้ายังอยู่ในระบบ')) return;
        await api('/sections/' + id + '/students/' + encodeURIComponent(s.id), {method: 'DELETE'});
        await load();
      }));
      action.append(remove);
      tr.append(el('td', 'id', s.id), el('td', null, s.name), face, action);
      body.append(tr);
    }
    const empty = $('#roster-empty');
    empty.hidden = rows.length > 0;
    empty.textContent = students.length ? 'ไม่พบรายชื่อที่ตรงกับตัวกรอง' : 'ยังไม่มีนักศึกษาในเซ็คนี้ นำเข้าจาก CSV หรือเพิ่มทีละคน';
  }

  function showSummary(summary) {
    const box = $('#import-result');
    box.replaceChildren();
    const lines = [
      ['พบในไฟล์', summary.rows + ' คน'],
      ['จะเพิ่มเข้าเซ็ค', summary.added + ' คน'],
      ['สร้างนักศึกษาใหม่ (ยังไม่มีใบหน้า)', summary.created + ' คน'],
      ['อยู่ในเซ็คแล้ว', summary.already_in_section + ' คน'],
    ];
    const dl = el('dl', 'facts');
    for (const [k, v] of lines) dl.append(el('dt', null, summary.dry_run ? k : k.replace('จะเพิ่ม', 'เพิ่ม').replace('สร้าง', 'สร้าง')), el('dd', 'num-left', v));
    box.append(dl);
    if (summary.name_conflicts.length) {
      box.append(el('p', 'small warn-text', 'ชื่อไม่ตรงกับที่มีอยู่ ' + summary.name_conflicts.length + ' คน ระบบจะใช้ชื่อเดิม เช่น ' +
        summary.name_conflicts.slice(0, 3).map(c => c.id + ': ' + c.current + ' / ไฟล์: ' + c.csv).join(' · ')));
    }
    if (summary.errors.length) {
      const list = el('ul', 'error-list');
      for (const e of summary.errors.slice(0, 8)) list.append(el('li', null, 'แถว ' + e.line + ': ' + e.error));
      if (summary.errors.length > 8) list.append(el('li', null, 'และอีก ' + (summary.errors.length - 8) + ' แถว'));
      box.append(el('p', 'small error-text', 'ข้ามแถวที่ไม่ถูกต้อง ' + summary.errors.length + ' แถว'), list);
    }
    box.hidden = false;
  }

  async function load() {
    const data = await json('/sections/' + id);
    section = data.section;
    students = data.students;
    render();
  }

  async function loadOptions() {
    const [roomData, userData] = await Promise.all([json('/rooms'), json('/users')]);
    const room = $('#edit-room'), teacher = $('#edit-teacher');
    room.replaceChildren(...roomData.rooms.map(r => option(r.id, r.code + (r.name ? ' · ' + r.name : ''))));
    teacher.replaceChildren(option('', 'ยังไม่กำหนด'), ...userData.users.map(u => option(u.id, u.name)));
    room.value = String(section.room_id);
    teacher.value = section.teacher_id ? String(section.teacher_id) : '';
    $('#edit-name').value = section.course_name || '';
  }

  $('#csv-file').addEventListener('change', () => run(async () => {
    const file = $('#csv-file').files[0];
    if (!file) return;
    if (file.size > 2 * 1024 * 1024) throw new Error('ไฟล์ใหญ่เกิน 2 MB');
    pending = await readFileBase64(file);
    const summary = await send('/sections/' + id + '/import', 'POST', {file: pending, dry_run: true});
    showSummary(summary);
    $('#import-actions').hidden = summary.added === 0;
    if (summary.added === 0) message('ไม่มีรายชื่อใหม่ให้เพิ่ม');
  }));
  $('#import-confirm').addEventListener('click', () => run(async () => {
    if (!pending) return;
    const summary = await send('/sections/' + id + '/import', 'POST', {file: pending});
    pending = null;
    $('#csv-file').value = '';
    $('#import-actions').hidden = true;
    showSummary(summary);
    message('นำเข้าแล้ว เพิ่ม ' + summary.added + ' คน');
    await load();
  }));
  $('#import-cancel').addEventListener('click', () => {
    pending = null;
    $('#csv-file').value = '';
    $('#import-result').hidden = true;
    $('#import-actions').hidden = true;
  });
  $('#add-form').addEventListener('submit', event => {
    event.preventDefault();
    run(async () => {
      const sid = $('#add-id').value.trim(), name = $('#add-name').value.trim().replace(/[",\r\n]/g, ' ');
      if (!sid || !name) throw new Error('ใส่รหัสและชื่อ');
      const summary = await send('/sections/' + id + '/import', 'POST', {csv: sid + ',' + name});
      if (summary.errors.length) throw new Error(summary.errors[0].error);
      $('#add-form').reset();
      message(summary.added ? 'เพิ่ม ' + sid + ' เข้าเซ็คแล้ว' : sid + ' อยู่ในเซ็คนี้แล้ว');
      await load();
    });
  });
  $('#edit-form').addEventListener('submit', event => {
    event.preventDefault();
    run(async () => {
      const teacher = $('#edit-teacher').value;
      await send('/sections/' + id, 'PATCH', {course_name: $('#edit-name').value, room_id: Number($('#edit-room').value),
                                              teacher_id: teacher ? Number(teacher) : null});
      message('บันทึกแล้ว');
      await load();
    });
  });
  $('#delete-section').addEventListener('click', () => run(async () => {
    if (!confirm('ลบเซ็คนี้? รายชื่อในเซ็คจะหายไป แต่นักศึกษาและภาพใบหน้ายังอยู่ในระบบ')) return;
    await api('/sections/' + id, {method: 'DELETE'});
    location.href = '/admin/sections';
  }));
  $('#roster-search').addEventListener('input', renderRoster);
  $('#missing-only').addEventListener('change', renderRoster);
  run(async () => {
    await load();
    await loadOptions();
    if (new URLSearchParams(location.search).get('new')) message('สร้างเซ็คแล้ว นำเข้ารายชื่อได้เลย');
  });
})();
