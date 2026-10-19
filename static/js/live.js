'use strict';
(() => {
  const {$, el, json, send, api, chip, run, message, pct, number, time, duration, BEHAVIOR} = App;
  const css = getComputedStyle(document.documentElement);
  const color = name => css.getPropertyValue('--b-' + name).trim() || '#6b7280';
  const LEGEND = ['attentive', 'inattentive', 'sleeping', 'phone', 'person'];
  const SHORT = {attentive: 'ตั้งใจ', inattentive: 'ไม่ตั้งใจ', sleeping: 'หลับ', phone: 'มือถือ', person: 'คน'};
  let data = null, bitmap = null, bitmapSeq = -1, mode = 'cira', filter = 'all', busy = false, lastError = '';
  let stream = null, scanning = false, scanResult = null;
  // Follow the session in the URL, else the user's latest open session (then pin it in the URL).
  let sessionId = App.sessionId;
  const query = () => sessionId ? '?session=' + sessionId : '';

  async function tick() {
    if (busy || document.hidden) return;
    busy = true;
    try {
      data = await json('/live' + query());
      if (!sessionId && data.session) {
        sessionId = data.session.id;
        history.replaceState(null, '', '/live?session=' + sessionId);
      }
      App.renderStatus({...data, open_sessions: 0});
      lastError = '';
      if (data.frame && data.frame.has_image && data.frame.seq !== bitmapSeq) {
        const response = await api('/live/frame' + query()).catch(() => null);
        if (response) {
          bitmap = await createImageBitmap(await response.blob());
          bitmapSeq = data.frame.seq;
        }
      }
      render();
    } catch (error) {
      if (error.message !== lastError) message(error.message, true);
      lastError = error.message;
    } finally {
      busy = false;
    }
  }

  function render() {
    const session = data.session;
    $('#no-session').hidden = !!session;
    $('#live-body').hidden = !session;
    if (!session) return stopCamera();
    if (session.ended_at && !render.endedShown) {
      render.endedShown = true;
      message('คาบนี้ปิดแล้ว ดูสรุปได้ที่หน้ารายงาน');
    }
    renderKpis();
    if (mode === 'cira') drawFrame();
    renderLegend();
    renderRoster();
  }

  function renderKpis() {
    const k = data.kpi;
    $('#kpi-present').textContent = k.present;
    $('#kpi-registered').textContent = '/' + k.registered;
    $('#kpi-faces').textContent = k.with_faces < k.registered
      ? 'มีภาพใบหน้า ' + k.with_faces + '/' + k.registered + ' คน'
      : 'จำหน้าได้และเช็คชื่อแล้ว';
    $('#kpi-attentive').textContent = pct(k.attentive_pct);
    // Head counts are averaged over the window; whole people read better than 4.9.
    $('#kpi-sleeping').textContent = count(k.sleeping);
    $('#kpi-people').textContent = count(k.people);
    $('#kpi-window').textContent = k.frames_in_window
      ? 'เฉลี่ย ' + k.window_seconds + ' วินาทีล่าสุด · ' + k.frames_in_window + ' เฟรม'
      : 'ยังไม่มีข้อมูลพฤติกรรม';
    $('#kpi-phone-card').hidden = !data.settings.phone_enabled;
    $('#kpi-phone').textContent = count(k.phone);
  }

  const count = value => value === null || value === undefined ? '—' : String(Math.round(value));

  // Draw the latest CiRA frame (or a blank stage when CiRA sent boxes only) plus every box.
  function drawFrame() {
    const canvas = $('#frame');
    const frame = data.frame;
    const aspect = frame ? frame.aspect : 16 / 9;
    $('#stage').style.aspectRatio = String(aspect);
    const ratio = window.devicePixelRatio || 1;
    const width = Math.round(canvas.clientWidth * ratio);
    const height = Math.round(width / aspect);
    if (canvas.width !== width || canvas.height !== height) { canvas.width = width; canvas.height = height; }
    const ctx = canvas.getContext('2d');
    ctx.clearRect(0, 0, width, height);
    ctx.fillStyle = '#eef0f3';
    ctx.fillRect(0, 0, width, height);
    const font = Math.round(12 * ratio) + 'px ' + css.getPropertyValue('--font');
    if (!frame) {
      centerText(ctx, 'รอข้อมูลจาก CiRA', width, height, font);
      $('#frame-meta').textContent = '';
      return;
    }
    if (frame.has_image && bitmap && bitmapSeq === frame.seq) ctx.drawImage(bitmap, 0, 0, width, height);
    else if (!frame.detections.length) centerText(ctx, 'เฟรมล่าสุดไม่พบคน', width, height, font);
    ctx.lineWidth = 2 * ratio;
    ctx.font = font;
    for (const d of frame.detections) {
      const name = SHORT[d.behavior] || d.label, confidence = Math.round(d.confidence * 100) + '%';
      // Longest label that fits the box width; colour alone carries the meaning when nothing fits.
      const options = [name + ' ' + confidence + (d.student_id ? ' · ' + d.student_id : ''), name + ' ' + confidence, name];
      const boxWidth = (d.box[2] - d.box[0]) * width;
      const label = options.find(text => ctx.measureText(text).width + 8 * ratio <= boxWidth) || null;
      drawBox(ctx, d.box, color(d.behavior), label, width, height, ratio);
    }
    ctx.setLineDash([4 * ratio, 3 * ratio]);
    ctx.lineWidth = 1.5 * ratio;
    for (const f of frame.faces) drawBox(ctx, f.box, color('face'), null, width, height, ratio);
    ctx.setLineDash([]);
    const source = frame.source === 'simulator' ? 'ข้อมูลจำลอง' : 'CiRA ' + frame.function;
    $('#frame-meta').textContent = source + ' · เฟรม #' + frame.seq + ' · ' + time(frame.received_at)
      + (frame.has_image ? ' · พบใบหน้า ' + frame.faces.length : ' · ไม่มีภาพแนบ แสดงเฉพาะตำแหน่งกรอบ');
  }

  function drawBox(ctx, box, stroke, label, width, height, ratio) {
    const [x0, y0, x1, y1] = [box[0] * width, box[1] * height, box[2] * width, box[3] * height];
    ctx.strokeStyle = stroke;
    ctx.strokeRect(x0, y0, x1 - x0, y1 - y0);
    if (!label) return;
    const pad = 4 * ratio, h = 18 * ratio, w = ctx.measureText(label).width + pad * 2;
    const y = y0 - h >= 0 ? y0 - h : y0;
    ctx.fillStyle = stroke;
    ctx.fillRect(x0 - ctx.lineWidth / 2, y, w, h);
    ctx.fillStyle = '#fff';
    ctx.textBaseline = 'middle';
    ctx.fillText(label, x0 + pad - ctx.lineWidth / 2, y + h / 2);
  }

  function centerText(ctx, text, width, height, font) {
    ctx.font = font;
    ctx.fillStyle = css.getPropertyValue('--text-3').trim();
    ctx.textAlign = 'center';
    ctx.textBaseline = 'middle';
    ctx.fillText(text, width / 2, height / 2);
    ctx.textAlign = 'start';
  }

  function renderLegend() {
    const legend = $('#legend');
    legend.replaceChildren();
    if (mode !== 'cira') return;
    const counts = {};
    for (const d of (data.frame ? data.frame.detections : [])) counts[d.behavior] = (counts[d.behavior] || 0) + 1;
    for (const b of LEGEND) {
      if (b === 'phone' && !data.settings.phone_enabled) continue;
      if (b === 'person' && !counts.person) continue;
      const item = el('span');
      item.dataset.b = b;
      item.append(el('i'), (BEHAVIOR[b] || b).replace(' (ไม่ระบุพฤติกรรม)', '') + ' ' + (counts[b] || 0));
      legend.append(item);
    }
    if (data.frame && data.frame.has_image) {
      const face = el('span');
      face.dataset.b = 'face';
      face.append(el('i'), 'ใบหน้าที่ตรวจพบ');
      legend.append(face);
    }
  }

  function renderRoster() {
    const roster = data.roster;
    const groups = {
      all: roster,
      present: roster.filter(r => r.present),
      absent: roster.filter(r => !r.present),
      sleeping: roster.filter(r => r.status === 'sleeping'),
    };
    for (const button of $('#filters').querySelectorAll('button')) {
      button.setAttribute('aria-pressed', String(button.dataset.filter === filter));
      button.querySelector('span').textContent = groups[button.dataset.filter].length;
    }
    $('#roster-total').textContent = 'ในเซ็ค ' + roster.length + ' คน';
    const body = $('#roster');
    body.replaceChildren();
    for (const r of groups[filter]) {
      const tr = el('tr');
      tr.append(el('td', 'id', r.id), el('td', null, r.name));
      const seen = el('td');
      seen.append(r.present ? el('span', null, time(r.seen_at)) : el('span', 'muted', 'ยังไม่พบ'));
      const status = el('td');
      if (['attentive', 'inattentive', 'sleeping', 'phone'].includes(r.status)) {
        const wrap = el('span', 'stack-cell');
        wrap.append(chip(r.status), el('span', 'muted', duration(r.status_seconds)));
        status.append(wrap);
      } else {
        status.append(el('span', 'muted', !r.has_face && !r.present ? 'ยังไม่มีภาพใบหน้า' : r.status === 'not_seen' ? 'ไม่เห็นในภาพ' : '—'));
      }
      const share = el('td', 'num', r.frames ? pct(r.shares.attentive * 100) : '—');
      tr.append(seen, status, share);
      body.append(tr);
    }
    const empty = $('#roster-empty');
    empty.hidden = groups[filter].length > 0;
    if (!roster.length) {
      empty.textContent = 'เซ็คนี้ยังไม่มีรายชื่อนักศึกษา ผู้ดูแลนำเข้าได้จากหน้า "ห้องและเซ็ค"';
    } else {
      empty.textContent = 'ไม่มีรายชื่อในกลุ่มนี้';
    }
  }

  // Optional browser-camera check-in (the original face pipeline), kept for rooms without CiRA.
  function setMode(next) {
    mode = next;
    for (const button of $('#modes').querySelectorAll('button')) button.setAttribute('aria-pressed', String(button.dataset.mode === mode));
    const camera = mode === 'camera';
    $('#frame').hidden = camera;
    $('#video').hidden = !camera;
    $('#overlay').hidden = !camera;
    $('#camera-controls').hidden = !camera;
    $('#stage-title').textContent = camera ? 'กล้องเครื่องนี้' : 'ภาพล่าสุดจาก CiRA';
    $('#frame-meta').textContent = camera ? 'เช็คชื่อด้วยใบหน้าจากกล้องของเบราว์เซอร์ ไม่ตรวจพฤติกรรม' : '';
    if (camera) $('#stage').style.aspectRatio = '4 / 3';
    else stopCamera();
    if (data) render();
  }

  function stopScan() {
    scanning = false;
    $('#scan-button').textContent = 'เริ่มเช็คชื่อ';
  }

  function stopCamera() {
    stopScan();
    if (stream) stream.getTracks().forEach(track => track.stop());
    stream = null;
    $('#video').srcObject = null;
    $('#camera-button').textContent = 'เปิดกล้อง';
    $('#scan-button').disabled = true;
    const overlay = $('#overlay');
    overlay.getContext('2d').clearRect(0, 0, overlay.width, overlay.height);
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

  async function scan() {
    if (!scanning || !App.session) return stopScan();
    try {
      scanResult = await send('/recognize', 'POST', {image: capture(), session_id: App.session.id});
      const overlay = $('#overlay');
      overlay.width = scanResult.width;
      overlay.height = scanResult.height;
      const ctx = overlay.getContext('2d');
      ctx.clearRect(0, 0, overlay.width, overlay.height);
      ctx.lineWidth = 2;
      ctx.font = '11px ' + css.getPropertyValue('--font');
      for (const face of scanResult.faces) {
        const [x, y, w, h] = face.box;
        ctx.strokeStyle = face.student_id ? color('face') : color('person');
        ctx.strokeRect(x, y, w, h);
        ctx.fillStyle = ctx.strokeStyle;
        ctx.fillText(face.student_id || 'ไม่รู้จัก', x, Math.max(12, y - 4));
      }
      $('#frame-meta').textContent = 'พบใบหน้า ' + scanResult.count + ' · จำได้ ' + scanResult.faces.filter(f => f.student_id).length;
    } catch (error) {
      stopScan();
      message(error.message, true);
    }
    if (scanning) setTimeout(scan, 1200);
  }

  $('#modes').addEventListener('click', event => {
    const button = event.target.closest('button');
    if (button) setMode(button.dataset.mode);
  });
  $('#filters').addEventListener('click', event => {
    const button = event.target.closest('button');
    if (!button) return;
    filter = button.dataset.filter;
    if (data) renderRoster();
  });
  $('#camera-button').addEventListener('click', () => run(async () => {
    if (stream) return stopCamera();
    stream = await navigator.mediaDevices.getUserMedia({video: {width: 640, height: 480}, audio: false});
    $('#video').srcObject = stream;
    await $('#video').play();
    $('#camera-button').textContent = 'ปิดกล้อง';
    $('#scan-button').disabled = false;
  }));
  $('#scan-button').addEventListener('click', () => {
    if (scanning) return stopScan();
    scanning = true;
    $('#scan-button').textContent = 'หยุดเช็คชื่อ';
    scan();
  });
  App.onSessionChange = () => { bitmap = null; tick(); };
  window.addEventListener('resize', () => { if (data && mode === 'cira' && data.session) drawFrame(); });
  window.addEventListener('beforeunload', stopCamera);
  tick();
  setInterval(tick, 1000);
})();
