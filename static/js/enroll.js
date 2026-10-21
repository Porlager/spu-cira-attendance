'use strict';
// Admin: take 3–8 face photos for a student from an imported roster (or a new student).
(() => {
  const {$, el, json, send, run, message} = App;
  const MIN = 3, MAX = 8;
  // images: [{src, ok}] where ok is null while the server is still checking the photo
  let stream = null, images = [], students = [], picked = null, creating = false, checking = false;
  const usable = () => images.filter(i => i.ok === true);

  function stopCamera() {
    if (stream) stream.getTracks().forEach(track => track.stop());
    stream = null;
    $('#video').srcObject = null;
    $('#video').hidden = true;
    $('#camera-empty').hidden = false;
    $('#camera-button').textContent = 'เปิดกล้อง';
    $('#camera-state').textContent = 'ปิดอยู่';
    $('#face-guide').hidden = true;
    $('#face-status').hidden = true;
    update();
  }

  // Live hint while the camera is on: is there exactly one clear face right now?
  async function watchFace() {
    if (!stream) return;
    if (!checking && !document.hidden && $('#video').videoWidth) {
      checking = true;
      try {
        const result = await send('/faces/check', 'POST', {image: capture()});
        showFace(result.ok, result.ok ? 'พบใบหน้า พร้อมถ่าย' : result.reason + ' · ' + (result.reason === 'ไม่พบใบหน้า' ? 'ขยับให้หน้าอยู่ในกรอบและมองกล้อง' : 'ให้มีคนเดียวในภาพ'));
      } catch (_) { /* keep the last hint */ } finally {
        checking = false;
      }
    }
    setTimeout(watchFace, 900);
  }

  function showFace(ok, text) {
    for (const id of ['#face-guide', '#face-status']) {
      $(id).hidden = !stream;
      $(id).dataset.state = ok ? 'ok' : 'bad';
    }
    $('#face-status').textContent = text;
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
    $('#sample-count').textContent = 'ใช้ได้ ' + usable().length + ' · ถ่ายแล้ว ' + images.length + ' / ' + MAX + ' ภาพ';
    $('#capture').disabled = !stream || images.length >= MAX;
    const missing = [];
    if (creating) {
      if (!/^[A-Za-z0-9_-]+$/.test($('#student-id').value.trim())) missing.push('ใส่รหัส (A-Z, 0-9, _ -)');
      if (!$('#student-name').value.trim()) missing.push('ใส่ชื่อ');
    } else if (!picked) {
      missing.push('เลือกนักศึกษา');
    }
    if (images.some(i => i.ok === null)) missing.push('กำลังตรวจภาพ');
    else if (usable().length < MIN) missing.push('ต้องมีภาพที่พบใบหน้าอีก ' + (MIN - usable().length) + ' ภาพ');
    if (!$('#consent').checked) missing.push('ติ๊กยินยอม');
    $('#save-student').disabled = missing.length > 0;
    $('#save-hint').textContent = missing.length ? 'ยังขาด: ' + missing.join(' · ') : 'พร้อมบันทึก';
  }

  function renderSamples() {
    const box = $('#samples');
    box.replaceChildren();
    images.forEach((photo, index) => {
      const item = el('div', 'thumb');
      item.dataset.state = photo.ok === null ? 'wait' : photo.ok ? 'ok' : 'bad';
      const img = el('img');
      img.src = photo.src;
      img.alt = 'ภาพที่ ' + (index + 1);
      const remove = el('button', null, 'ลบ');
      remove.type = 'button';
      remove.addEventListener('click', () => { images.splice(index, 1); renderSamples(); });
      const state = el('span', 'thumb-state', photo.ok === null ? 'กำลังตรวจ' : photo.ok ? 'พบใบหน้า' : photo.reason);
      item.append(img, remove, state);
      box.append(item);
    });
    update();
  }

  function renderResults() {
    const query = $('#student-search').value.trim().toLowerCase();
    const list = $('#student-results');
    list.replaceChildren();
    const rows = students
      .filter(s => !query || s.id.toLowerCase().includes(query) || s.name.toLowerCase().includes(query))
      .sort((a, b) => (a.samples > 0) - (b.samples > 0) || a.id.localeCompare(b.id))
      .slice(0, 8);
    for (const s of rows) {
      const item = el('li');
      const pick = el('button', 'pick');
      pick.type = 'button';
      pick.append(el('span', 'pick-title', s.id + ' · ' + s.name),
        el('span', 'muted small', (s.samples ? 'มีใบหน้าแล้ว ' + s.samples + ' ภาพ (ถ่ายใหม่จะแทนที่)' : 'ยังไม่มีใบหน้า') + (s.sections ? ' · ' + s.sections : '')));
      pick.addEventListener('click', () => choose(s));
      item.append(pick);
      list.append(item);
    }
    if (!rows.length) list.append(el('li', 'empty', students.length ? 'ไม่พบ ลองพิมพ์รหัสอื่น' : 'ยังไม่มีรายชื่อ นำเข้าจากหน้า "ห้องและเซ็ค" หรือเพิ่มคนใหม่'));
  }

  function choose(student) {
    picked = student;
    creating = false;
    $('#pick-block').hidden = true;
    $('#new-block').hidden = true;
    $('#picked').hidden = false;
    $('#picked-name').textContent = student.id + ' · ' + student.name;
    $('#picked-meta').textContent = (student.samples ? 'มีใบหน้าแล้ว ' + student.samples + ' ภาพ ภาพใหม่จะแทนที่ของเดิม' : 'ยังไม่มีภาพใบหน้า') +
      (student.sections ? ' · ' + student.sections : '');
    update();
  }

  function showSearch() {
    picked = null;
    creating = false;
    $('#pick-block').hidden = false;
    $('#new-block').hidden = true;
    $('#picked').hidden = true;
    renderResults();
    update();
  }

  async function load() {
    students = (await json('/students')).students;
    const missing = students.filter(s => !s.samples).length;
    $('#missing-count').textContent = missing ? 'ยังไม่มีใบหน้า ' + missing + ' คน' : '';
    const wanted = new URLSearchParams(location.search).get('student');
    const found = wanted && students.find(s => s.id === wanted);
    if (found) choose(found); else showSearch();
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
    watchFace();
  }));
  $('#capture').addEventListener('click', () => run(async () => {
    if (images.length >= MAX) return;
    const photo = {src: capture(), ok: null};
    images.push(photo);
    renderSamples();
    try {
      const result = await send('/faces/check', 'POST', {image: photo.src});
      photo.ok = result.ok;
      photo.reason = result.reason || '';
    } catch (error) {
      photo.ok = false;
      photo.reason = 'ตรวจไม่สำเร็จ';
    }
    renderSamples();
    if (!photo.ok) message('ภาพนี้' + photo.reason + ' จะไม่ถูกใช้ ลบแล้วถ่ายใหม่ได้', true);
  }));
  $('#student-search').addEventListener('input', renderResults);
  $('#change-student').addEventListener('click', showSearch);
  $('#back-to-search').addEventListener('click', showSearch);
  $('#new-student').addEventListener('click', () => {
    picked = null;
    creating = true;
    $('#pick-block').hidden = true;
    $('#picked').hidden = true;
    $('#new-block').hidden = false;
    update();
  });
  for (const id of ['#student-id', '#student-name', '#consent']) $(id).addEventListener('input', update);
  $('#enroll-form').addEventListener('submit', event => {
    event.preventDefault();
    run(async () => {
      $('#save-student').disabled = true;
      try {
        const good = usable().map(i => i.src);
        const result = creating
          ? await send('/students', 'POST', {id: $('#student-id').value.trim(), name: $('#student-name').value.trim(), consent: true, images: good})
          : await send('/students/' + encodeURIComponent(picked.id) + '/faces', 'POST', {consent: $('#consent').checked, images: good});
        message('บันทึกใบหน้า ' + result.id + ' แล้ว (' + result.samples + ' ภาพ' + (result.skipped.length ? ' ข้าม ' + result.skipped.length + ' ภาพที่ไม่ชัด' : '') + ')');
        images = [];
        $('#enroll-form').reset();
        renderSamples();
        history.replaceState(null, '', '/enroll');
        await load();
      } finally {
        update();
      }
    });
  });
  window.addEventListener('beforeunload', stopCamera);
  run(load);
})();
