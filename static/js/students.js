'use strict';
(() => {
  const {$, el, link, json, api, run, message} = App;
  let students = [], filter = 'all';

  function render() {
    const query = $('#search').value.trim().toLowerCase();
    const groups = {all: students, missing: students.filter(s => !s.samples)};
    for (const button of $('#filters').querySelectorAll('button')) {
      button.setAttribute('aria-pressed', String(button.dataset.filter === filter));
      button.querySelector('span').textContent = groups[button.dataset.filter].length;
    }
    const rows = groups[filter].filter(s => !query || [s.id, s.name, s.sections || ''].some(v => v.toLowerCase().includes(query)));
    $('#student-total').textContent = students.length ? '(' + students.length + ')' : '';
    const body = $('#students');
    body.replaceChildren();
    for (const s of rows) {
      const tr = el('tr');
      const face = el('td');
      if (s.samples) face.append(App.chip('attentive', 'มี ' + s.samples + ' ภาพ'));
      else face.append(link('/enroll?student=' + encodeURIComponent(s.id), 'ยังไม่มี · ถ่ายภาพ', 'chip-link'));
      const actions = el('td', 'actions');
      const remove = el('button', 'btn btn-danger btn-sm', 'ลบ');
      remove.type = 'button';
      remove.addEventListener('click', () => run(async () => {
        if (!confirm('ลบ ' + s.id + ' ' + s.name + '?\nภาพใบหน้า รายชื่อในทุกเซ็ค ประวัติเช็คชื่อ และพฤติกรรมทุกคาบจะถูกลบถาวร')) return;
        await api('/students/' + encodeURIComponent(s.id), {method: 'DELETE'});
        message('ลบ ' + s.id + ' แล้ว');
        await load();
      }));
      actions.append(remove);
      tr.append(el('td', 'id', s.id), el('td', null, s.name), el('td', s.sections ? 'small' : 'muted', s.sections || '—'),
        face, el('td', 'num', s.sessions_attended), actions);
      body.append(tr);
    }
    const empty = $('#students-empty');
    empty.hidden = rows.length > 0;
    empty.textContent = students.length ? 'ไม่พบรายชื่อที่ตรงกับตัวกรอง' : 'ยังไม่มีนักศึกษา นำเข้ารายชื่อจาก CSV ในหน้า "ห้องและเซ็ค"';
  }

  async function load() {
    students = (await json('/students')).students;
    render();
  }

  $('#search').addEventListener('input', render);
  $('#filters').addEventListener('click', event => {
    const button = event.target.closest('button');
    if (!button) return;
    filter = button.dataset.filter;
    render();
  });
  run(load);
})();
