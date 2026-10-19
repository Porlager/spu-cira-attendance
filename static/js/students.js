'use strict';
(() => {
  const {$, el, json, api, run, message, dateTime} = App;
  let students = [];

  function render() {
    const query = $('#search').value.trim().toLowerCase();
    const rows = students.filter(s => !query || s.id.toLowerCase().includes(query) || s.name.toLowerCase().includes(query));
    $('#student-total').textContent = students.length ? '(' + students.length + ')' : '';
    const body = $('#students');
    body.replaceChildren();
    for (const s of rows) {
      const tr = el('tr');
      const actions = el('td', 'actions');
      const remove = el('button', 'btn btn-danger btn-sm', 'ลบ');
      remove.type = 'button';
      remove.addEventListener('click', () => run(async () => {
        if (!confirm('ลบ ' + s.id + ' ' + s.name + '?\nภาพใบหน้า ประวัติเช็คชื่อ และพฤติกรรมทุกคาบจะถูกลบถาวร')) return;
        await api('/students/' + encodeURIComponent(s.id), {method: 'DELETE'});
        message('ลบ ' + s.id + ' แล้ว');
        await load();
      }));
      actions.append(remove);
      tr.append(el('td', 'id', s.id), el('td', null, s.name), el('td', 'num', s.samples),
        el('td', 'num', s.sessions_attended), el('td', 'muted', dateTime(s.created_at)), actions);
      body.append(tr);
    }
    const empty = $('#students-empty');
    empty.hidden = rows.length > 0;
    empty.textContent = students.length ? 'ไม่พบ "' + query + '"' : 'ยังไม่มีนักศึกษาลงทะเบียน';
  }

  async function load() {
    students = (await json('/students')).students;
    render();
  }

  $('#search').addEventListener('input', render);
  run(load);
})();
