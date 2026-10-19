'use strict';
(() => {
  const {$, el, send, run, message} = App;
  const MIN = 3, MAX = 8;
  let stream = null, images = [];

  function stopCamera() {
    if (stream) stream.getTracks().forEach(track => track.stop());
    stream = null;
    $('#video').srcObject = null;
    $('#video').hidden = true;
    $('#camera-empty').hidden = false;
    $('#camera-button').textContent = 'เปิดกล้อง';
    $('#camera-state').textContent = 'ปิดอยู่';
    update();
  }

  function capture() {
    const video = $('#video');
    if (!stream || !video.videoWidth) throw new Error('เปิดกล้องและรอภาพก่อน');
    const canvas = el('canvas');
    canvas.width = 640;
    canvas.height = Math.round(640 * video.videoHeight / video.videoWidth);
    canvas.getContext('2d').drawImage(video, 0, 0, canvas.width, canvas.height);
    return canvas.toDataURL('image/jpeg', 0.85);
  }

  function update() {
    $('#sample-count').textContent = images.length + ' / ' + MAX + ' ภาพ';
    $('#capture').disabled = !stream || images.length >= MAX;
    const id = $('#student-id').value.trim(), name = $('#student-name').value.trim();
    const missing = [];
    if (images.length < MIN) missing.push('ถ่ายอีก ' + (MIN - images.length) + ' ภาพ');
    if (!id || !/^[A-Za-z0-9_-]+$/.test(id)) missing.push('ใส่รหัส (A-Z, 0-9, _ -)');
    if (!name) missing.push('ใส่ชื่อ');
    if (!$('#consent').checked) missing.push('ติ๊กยินยอม');
    $('#save-student').disabled = missing.length > 0;
    $('#save-hint').textContent = missing.length ? 'ยังขาด: ' + missing.join(' · ') : 'พร้อมบันทึก';
  }

  function renderSamples() {
    const box = $('#samples');
    box.replaceChildren();
    images.forEach((src, index) => {
      const item = el('div', 'thumb');
      const img = el('img');
      img.src = src;
      img.alt = 'ภาพที่ ' + (index + 1);
      const remove = el('button', null, 'ลบ');
      remove.type = 'button';
      remove.addEventListener('click', () => { images.splice(index, 1); renderSamples(); });
      item.append(img, remove);
      box.append(item);
    });
    update();
  }

  $('#camera-button').addEventListener('click', () => run(async () => {
    if (stream) return stopCamera();
    stream = await navigator.mediaDevices.getUserMedia({video: {width: 640, height: 480}, audio: false});
    $('#video').srcObject = stream;
    $('#video').hidden = false;
    $('#camera-empty').hidden = true;
    await $('#video').play();
    $('#camera-button').textContent = 'ปิดกล้อง';
    $('#camera-state').textContent = 'กำลังใช้งาน';
    update();
  }));
  $('#capture').addEventListener('click', () => run(async () => {
    if (images.length >= MAX) return;
    images.push(capture());
    renderSamples();
  }));
  for (const id of ['#student-id', '#student-name', '#consent']) $(id).addEventListener('input', update);
  $('#enroll-form').addEventListener('submit', event => {
    event.preventDefault();
    run(async () => {
      $('#save-student').disabled = true;
      try {
        const result = await send('/students', 'POST', {
          id: $('#student-id').value.trim(), name: $('#student-name').value.trim(), consent: $('#consent').checked, images,
        });
        message('บันทึกใบหน้า ' + result.id + ' แล้ว (' + result.samples + ' ภาพ)');
        images = [];
        $('#enroll-form').reset();
        renderSamples();
      } finally {
        update();
      }
    });
  });
  window.addEventListener('beforeunload', stopCamera);
  update();
})();
