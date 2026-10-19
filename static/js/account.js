'use strict';
(() => {
  const {$, send, run, message} = App;
  $('#password-form').addEventListener('submit', event => {
    event.preventDefault();
    run(async () => {
      if ($('#pw-new').value !== $('#pw-confirm').value) throw new Error('รหัสผ่านใหม่สองช่องไม่ตรงกัน');
      await send('/me/password', 'POST', {current: $('#pw-current').value, new: $('#pw-new').value});
      $('#password-form').reset();
      message('เปลี่ยนรหัสผ่านแล้ว');
    });
  });
})();
