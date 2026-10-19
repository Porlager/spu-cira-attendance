'use strict';
// Admin: teacher and admin accounts.
(() => {
  const {$, el, json, send, api, run, message} = App;
  const ROLE = {admin: 'ผู้ดูแล', teacher: 'อาจารย์'};

  async function load() {
    const {users} = await json('/users');
    $('#user-count').textContent = users.length + ' บัญชี';
    const body = $('#users');
    body.replaceChildren();
    for (const u of users) {
      const tr = el('tr');
      const actions = el('td', 'actions');
      const reset = el('button', 'btn btn-secondary btn-sm', 'ตั้งรหัสผ่านใหม่');
      reset.type = 'button';
      reset.addEventListener('click', () => run(async () => {
        const password = prompt('รหัสผ่านใหม่ของ ' + u.name + ' (อย่างน้อย 8 ตัว)');
        if (!password) return;
        await send('/users/' + u.id, 'PATCH', {password});
        message('ตั้งรหัสผ่านใหม่ให้ ' + u.name + ' แล้ว');
      }));
      actions.append(reset);
      if (u.id !== App.uid) {
        const remove = el('button', 'btn btn-danger btn-sm', 'ลบ');
        remove.type = 'button';
        remove.addEventListener('click', () => run(async () => {
          if (!confirm('ลบบัญชี ' + u.name + '?' + (u.sections ? '\nเซ็คที่สอนอยู่ ' + u.sections + ' เซ็คจะไม่มีอาจารย์ผู้สอน' : ''))) return;
          await api('/users/' + u.id, {method: 'DELETE'});
          await load();
        }));
        actions.append(remove);
      }
      tr.append(el('td', null, u.name + (u.id === App.uid ? ' (คุณ)' : '')), el('td', 'id', u.username),
        el('td', null, ROLE[u.role]), el('td', 'num', u.sections), actions);
      body.append(tr);
    }
  }

  $('#user-form').addEventListener('submit', event => {
    event.preventDefault();
    run(async () => {
      await send('/users', 'POST', {name: $('#u-name').value, username: $('#u-username').value,
                                   role: $('#u-role').value, password: $('#u-password').value});
      message('เพิ่มบัญชีแล้ว แจ้งชื่อผู้ใช้และรหัสผ่านให้เจ้าของบัญชีโดยตรง');
      $('#user-form').reset();
      await load();
    });
  });
  run(load);
})();
