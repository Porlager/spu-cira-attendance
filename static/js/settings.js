'use strict';
(() => {
  const {$, el, json, send, run, message, time} = App;
  const TARGETS = {
    attentive: 'ตั้งใจเรียน', inattentive: 'ไม่ตั้งใจ', sleeping: 'หลับ', phone: 'ใช้โทรศัพท์ (ทดลอง)',
    person: 'คน (นับจำนวนอย่างเดียว)', ignore: 'ไม่นับ',
  };
  const NUMBERS = ['confidence_scale', 'min_confidence', 'smoothing_frames'];
  let behaviors = Object.keys(TARGETS), unmapped = [];

  function addRow(name = '', target = 'attentive') {
    const tr = el('tr');
    const input = el('input');
    input.type = 'text';
    input.value = name;
    input.maxLength = 64;
    input.placeholder = 'เช่น sleep';
    input.setAttribute('aria-label', 'ชื่อคลาสจาก CiRA');
    input.addEventListener('input', renderUnmapped);
    const select = el('select');
    select.setAttribute('aria-label', 'พฤติกรรม');
    for (const b of behaviors) {
      const option = el('option', null, TARGETS[b] || b);
      option.value = b;
      select.append(option);
    }
    select.value = target;
    const remove = el('button', 'btn btn-danger btn-sm', 'ลบ');
    remove.type = 'button';
    remove.addEventListener('click', () => { tr.remove(); renderUnmapped(); });
    const cells = [input, select, remove].map(control => { const td = el('td'); td.append(control); return td; });
    tr.append(...cells);
    $('#mapping').append(tr);
    return input;
  }

  function mappedNames() {
    return new Set([...$('#mapping').querySelectorAll('input')].map(input => input.value.trim().toLowerCase()).filter(Boolean));
  }

  function renderUnmapped() {
    const names = mappedNames();
    const items = unmapped.filter(u => !names.has(u.label));
    const box = $('#unmapped');
    box.replaceChildren();
    $('#unmapped-box').hidden = !items.length;
    for (const u of items) {
      const button = el('button', null, '+ ' + u.label);
      button.type = 'button';
      button.title = 'ได้รับล่าสุด ' + time(u.last_seen);
      button.addEventListener('click', () => {
        addRow(u.label, 'ignore').closest('tr').querySelector('select').focus();
        renderUnmapped();
      });
      box.append(button);
    }
  }

  function example(data) {
    return [
      '# ใน Python node ของ CiRA CORE',
      'import sys',
      'sys.path.append(' + JSON.stringify(data.integrations_dir) + ')',
      'from cira_bridge import send_detections',
      '',
      '# payload = ผลจาก DeepDetect  เช่น {"DeepDetect": {"objects": [...]}}',
      '# frame   = ภาพ BGR เฟรมเดียวกัน ไม่ crop (ไม่ใส่ก็ได้ แต่จะเช็คชื่อรายคนไม่ได้)',
      'send_detections(payload,',
      '                token_file=' + JSON.stringify(data.token_file) + ',',
      '                frame_bgr=frame)',
      '',
      '# หรือ HTTP POST ' + data.endpoint,
      '# {"function": "DeepDetect", "width": 1280, "height": 720,',
      '#  "detections": [{"name": "sleep", "confidence": 0.82,',
      '#                  "x": 100, "y": 120, "w": 80, "h": 90}]}',
    ].join('\n');
  }

  function fill(data) {
    const s = data.settings;
    behaviors = data.behaviors;
    unmapped = data.unmapped_labels;
    $('#mapping').replaceChildren();
    for (const [name, target] of Object.entries(s.label_map)) addRow(name, target);
    for (const key of ['box_origin', 'coord_units', ...NUMBERS]) $('#' + key).value = s[key];
    $('#phone_enabled').checked = s.phone_enabled;
    $('#endpoint').textContent = data.endpoint;
    $('#token-file').textContent = data.token_file;
    $('#example').textContent = example(data);
    renderUnmapped();
  }

  function collect() {
    const label_map = {};
    for (const tr of $('#mapping').querySelectorAll('tr')) {
      const name = tr.querySelector('input').value.trim().toLowerCase();
      if (!name) continue;
      if (label_map[name]) throw new Error('ชื่อคลาส "' + name + '" ซ้ำกัน');
      label_map[name] = tr.querySelector('select').value;
    }
    const data = {label_map, box_origin: $('#box_origin').value, coord_units: $('#coord_units').value,
                  phone_enabled: $('#phone_enabled').checked};
    for (const key of NUMBERS) data[key] = Number($('#' + key).value);
    return data;
  }

  async function load() {
    fill(await json('/settings'));
  }

  async function poll() {
    const [outputs, settings, status] = await Promise.all([json('/cira/outputs'), json('/settings'), json('/status')]);
    App.renderStatus(status);
    $('#conn-status').textContent = $('#cira-text').textContent;
    unmapped = settings.unmapped_labels;
    renderUnmapped();
    const box = $('#outputs');
    if (!outputs.outputs.length) return;
    box.replaceChildren();
    for (const o of outputs.outputs) {
      const item = el('div', 'output');
      const head = el('div', 'output-head');
      head.append(el('strong', null, o.function), el('span', 'muted', time(o.received_at)));
      let text = JSON.stringify(o.output, null, 2);
      if (text.length > 4000) text = text.slice(0, 4000) + '\n… (ตัดให้สั้นลง)';
      item.append(head, el('pre', 'code', text));
      box.append(item);
    }
  }

  $('#add-row').addEventListener('click', () => addRow('', 'attentive').focus());
  $('#settings-form').addEventListener('submit', event => {
    event.preventDefault();
    run(async () => {
      await send('/settings', 'PUT', collect());
      await load();
      message('บันทึกการตั้งค่าแล้ว มีผลกับเฟรมถัดไปทันที');
    });
  });
  run(load);
  run(poll);
  setInterval(() => { if (!document.hidden) poll().catch(() => {}); }, 3000);
})();
