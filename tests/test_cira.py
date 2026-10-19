import csv
import io
import json
import re
import sqlite3
import behavior as bh
from app import create_app
from test_app import FakeVision, client, enroll, start  # noqa: F401  (client is a fixture)


def make_client(tmp_path):
    c = create_app(tmp_path, FakeVision()).test_client()
    c.environ_base['HTTP_X_API_KEY'] = (tmp_path/'api-token.txt').read_text()
    return c


def d(name, x=0, y=0, w=128, h=120, confidence=0.9, **extra):
    return dict(name=name, confidence=confidence, x=x, y=y, w=w, h=h, **extra)


def frame(c, detections, **extra):
    payload = dict(detections=detections, width=640, height=480)
    payload.update(extra)
    return c.post('/api/cira/frame', json=payload)


def put(c, **settings):
    return c.put('/api/settings', json=settings)


def start_sim(c):
    return c.post('/api/sessions', json={'title': 'คาบจำลอง', 'simulated': True}).json['id']


def test_pages_render(client):
    for path in ['/', '/enroll', '/students', '/reports', '/settings']:
        response = client.get(path)
        assert response.status_code == 200, path
        html = response.data.decode()
        assert 'name="api-key"' in html
        assert "script-src 'self'" in response.headers['Content-Security-Policy']
        assert 'style="' not in html and not re.search(r'\son\w+=', html)
        assert not re.search(r'<script(?![^>]*\bsrc=)', html)


def test_settings_roundtrip_and_validation(tmp_path):
    c = make_client(tmp_path)
    data = c.get('/api/settings').json
    assert data['settings'] == bh.DEFAULT_SETTINGS
    assert data['token_file'].endswith('api-token.txt')
    assert (tmp_path/'api-token.txt').read_text() not in json.dumps(data)
    assert put(c, label_map={'Head_Down': 'sleeping'}, phone_enabled=True).status_code == 200
    for bad in [dict(label_map={'x': 'dead'}), dict(box_origin='corner'), dict(phone_enabled='yes'), dict(unknown=1)]:
        assert put(c, **bad).status_code == 400
    again = make_client(tmp_path).get('/api/settings').json['settings']
    assert again['label_map'] == {'head_down': 'sleeping'} and again['phone_enabled'] is True


def test_frame_box_normalization(client):
    start(client)
    frame(client, [d('sleep', 64, 48, 128, 96)])
    assert client.get('/api/live').json['frame']['detections'][0]['box'] == [0.1, 0.1, 0.3, 0.3]
    put(client, box_origin='center')
    frame(client, [d('sleep', 128, 96, 128, 96)])
    assert client.get('/api/live').json['frame']['detections'][0]['box'] == [0.1, 0.1, 0.3, 0.3]
    put(client, coord_units='normalized')
    frame(client, [d('sleep', .2, .2, .2, .2)], width=None, height=None)
    assert client.get('/api/live').json['frame']['detections'][0]['box'] == [0.1, 0.1, 0.3, 0.3]
    put(client, coord_units='pixel')
    assert client.post('/api/cira/frame', json={'detections': [d('sleep')]}).status_code == 400


def test_frame_counts_attentiveness_and_phone_toggle(client):
    start(client)
    items = [d('awake')] * 3 + [d('sleep'), d('phone'), d('chair')]
    result = frame(client, items).json
    assert result['people'] == 5 and result['attentive_pct'] == 75.0 and result['ignored_labels'] == ['chair']
    assert [u['label'] for u in client.get('/api/settings').json['unmapped_labels']] == ['chair']
    kpi = client.get('/api/live').json['kpi']
    assert kpi['attentive_pct'] == 75.0 and kpi['sleeping'] == 1 and kpi['phone'] is None and kpi['people'] == 5
    latest = client.get('/api/state').json['latest_count']
    assert latest['count'] == 5 and latest['source'] == 'cira'
    put(client, phone_enabled=True)
    assert frame(client, items).json['attentive_pct'] == 60.0
    assert client.get('/api/live').json['kpi']['phone'] == 0.5
    put(client, label_map={'chair': 'ignore'})
    assert client.get('/api/settings').json['unmapped_labels'] == []


def test_confidence_scale(client):
    start(client)
    assert frame(client, [d('sleep', confidence=95)]).status_code == 400
    put(client, confidence_scale=100)
    result = frame(client, [d('sleep', confidence=90), d('awake', confidence=30)]).json
    assert result['counts']['sleeping'] == 1 and result['people'] == 1


def test_frame_session_rules(client):
    assert frame(client, [d('sleep')]).status_code == 400
    cira = client.get('/api/status').json['cira']
    assert cira['status'] == 'error' and 'คาบ' in cira['last_error']
    sid = start(client)
    assert frame(client, [d('sleep')], session_id=sid + 1).status_code == 400
    assert frame(client, [d('sleep')], session_id=str(sid)).status_code == 400
    for bad in [dict(source='camera'), dict(function='bad/name')]:
        assert frame(client, [d('sleep')], **bad).status_code == 400
    assert frame(client, 'x').status_code == 400
    assert frame(client, [d('sleep')]).json['session_id'] == sid
    assert client.get('/api/status').json['cira']['status'] == 'live'
    client.post(f'/api/sessions/{sid}/close')
    assert frame(client, [d('sleep')], session_id=sid).status_code == 400


def test_frame_image_recognizes_and_attributes(client, tmp_path):
    enroll(client)
    start(client)
    # FakeVision finds one face at (1,2,50,50) in a 320x240 frame: center ~ (0.08, 0.11).
    result = frame(client, [d('sleep', 0, 0, 128, 120)], image='face').json
    assert result['faces'] == [{'student_id': '67000001', 'distance': 10, 'behavior': 'sleeping'}]
    assert client.get('/api/state').json['attendance'][0]['source'] == 'cira-face'
    con = sqlite3.connect(tmp_path/'attendance.sqlite3')
    assert con.execute('SELECT behavior, frames FROM student_behavior').fetchall() == [('sleeping', 1)]
    assert frame(client, [d('sleep', 400, 300, 100, 100)], image='face').json['faces'][0]['behavior'] is None
    live = client.get('/api/live').json
    assert live['frame']['has_image'] and live['frame']['faces'][0]['student_id'] == '67000001'
    assert live['roster'][0]['frames'] == 1 and live['roster'][0]['shares']['sleeping'] == 1


def test_smoothing_via_api(client):
    enroll(client)
    start(client)
    for expected in ['unknown', 'unknown', 'sleeping']:
        frame(client, [d('sleep', 0, 0, 128, 120)], image='face')
        assert client.get('/api/live').json['roster'][0]['status'] == expected


def test_simulator_isolation(client):
    enroll(client)
    sid = start(client)
    assert frame(client, [d('sleep')], source='simulator').status_code == 400
    assert frame(client, [d('sleep', student_id='67000001')]).status_code == 400
    client.post(f'/api/sessions/{sid}/close')
    assert client.post('/api/sessions', json={'title': 'x', 'simulated': 'yes'}).status_code == 400
    sim = start_sim(client)
    assert frame(client, [d('sleep')]).status_code == 400
    assert frame(client, [d('sleep', student_id='67000001')], source='simulator').status_code == 200
    assert client.get('/api/state').json['attendance'][0]['source'] == 'simulator'
    assert client.get('/api/live').json['roster'][0]['frames'] == 1
    assert f'attendance-{sim}-simulated.csv' in client.get(f'/api/sessions/{sim}/export').headers['Content-Disposition']


def test_live_frame_memory_only(client, tmp_path):
    enroll(client)
    sid = start(client)
    assert client.get('/api/live/frame').status_code == 404
    frame(client, [d('sleep')], image='face')
    response = client.get('/api/live/frame')
    assert response.status_code == 200 and response.mimetype == 'image/jpeg' and response.data[:2] == b'\xff\xd8'
    assert sorted(p.name for p in tmp_path.iterdir()) == ['api-token.txt', 'attendance.sqlite3']
    con = sqlite3.connect(tmp_path/'attendance.sqlite3')
    assert con.execute('SELECT COUNT(*) FROM samples').fetchone()[0] == 3
    client.post(f'/api/sessions/{sid}/close')
    assert client.get('/api/live/frame').status_code == 404
    assert client.get('/api/live').json['frame'] is None


def test_cira_outputs(client):
    assert client.post('/api/cira/output', json={'function': 'DeepClassify', 'output': {'name': 'sleep'}}).status_code == 200
    outputs = client.get('/api/cira/outputs').json['outputs']
    assert outputs[0]['function'] == 'DeepClassify' and outputs[0]['output'] == {'name': 'sleep'}
    assert client.get('/api/status').json['cira']['status'] == 'connected'
    for bad in [{'function': 'a/b', 'output': 1}, {'function': 'Big', 'output': 'x' * 70000}, {'function': 'NoOutput'}]:
        assert client.post('/api/cira/output', json=bad).status_code == 400


def test_report_and_csv_behavior_columns(client):
    enroll(client)
    enroll(client, id='67000002', name='B')
    sid = start(client)
    frame(client, [d('sleep', 0, 0, 128, 120), d('awake', 300, 300, 50, 50)], image='face')
    frame(client, [d('awake', 0, 0, 128, 120)], image='face')
    report = client.get(f'/api/sessions/{sid}/report').json
    assert report['summary']['frames'] == 2 and report['summary']['avg_attentive_pct'] == 66.7
    assert sum(t['frames'] for t in report['timeline']) == 2
    first = report['students'][0]
    assert first['frames'] == 2 and first['shares']['sleeping'] == 0.5 and first['present']
    rows = list(csv.reader(io.StringIO(client.get(f'/api/sessions/{sid}/export').data.decode('utf-8-sig'))))
    assert rows[0] == ['รหัสนักศึกษา', 'ชื่อ', 'สถานะ', 'เวลา UTC', 'แหล่งที่มา',
                       'เฟรมที่ระบุพฤติกรรมได้', 'ตั้งใจเรียน (%)', 'ไม่ตั้งใจ (%)', 'หลับ (%)']
    assert rows[1][5:] == ['2', '50.0', '0.0', '50.0'] and rows[2][5:] == ['0', '', '', '']
    assert client.get('/api/sessions/999/report').status_code == 404
    put(client, phone_enabled=True)
    header = next(csv.reader(io.StringIO(client.get(f'/api/sessions/{sid}/export').data.decode('utf-8-sig'))))
    assert header[-1] == 'ใช้โทรศัพท์ (%)'


def test_delete_student_removes_behavior(client, tmp_path):
    enroll(client)
    start(client)
    frame(client, [d('sleep', 0, 0, 128, 120)], image='face')
    client.delete('/api/students/67000001')
    con = sqlite3.connect(tmp_path/'attendance.sqlite3')
    assert con.execute('SELECT COUNT(*) FROM student_behavior').fetchone()[0] == 0
    assert con.execute('SELECT COUNT(*) FROM observations').fetchone()[0] == 1


def test_students_and_sessions_lists(client):
    enroll(client)
    sid = start(client)
    frame(client, [d('sleep', 0, 0, 128, 120)], image='face')
    student = client.get('/api/students').json['students'][0]
    assert student['samples'] == 3 and student['sessions_attended'] == 1
    session = client.get('/api/sessions').json['sessions'][0]
    assert session['id'] == sid and session['frames'] == 1 and session['present'] == 1 and session['simulated'] == 0


def test_migration_adds_simulated_column(tmp_path):
    con = sqlite3.connect(tmp_path/'attendance.sqlite3')
    con.execute('CREATE TABLE sessions(id INTEGER PRIMARY KEY, title TEXT NOT NULL, started_at TEXT NOT NULL, ended_at TEXT)')
    con.execute("INSERT INTO sessions(title, started_at) VALUES('old', '2026-01-01T00:00:00+00:00')")
    con.commit()
    con.close()
    assert make_client(tmp_path).get('/api/sessions').json['sessions'][0]['simulated'] == 0


def test_bridge_send_detections(monkeypatch):
    import numpy as np
    import pytest
    from integrations import cira_bridge
    calls = []
    monkeypatch.setattr(cira_bridge, 'post', lambda *args: calls.append(args) or {'saved': True})
    obj = {'name': 'sleep', 'confidence': .8, 'x': 1, 'y': 2, 'w': 3, 'h': 4}
    payload = {'DeepDetect': {'objects': [dict(obj, extra=1)]}}
    assert cira_bridge.send_detections(payload, 'unused', session_id=1, width=640, height=480, min_interval=0) == {'saved': True}
    path, body = calls[0][0], calls[0][1]
    assert path == '/api/cira/frame' and body['detections'] == [obj]
    assert body['width'] == 640 and body['height'] == 480 and body['session_id'] == 1 and 'image' not in body
    cira_bridge.send_detections([obj], 'unused', frame_bgr=np.zeros((480, 640, 3), np.uint8), function='Other', min_interval=0)
    assert calls[1][1]['width'] == 640 and calls[1][1]['height'] == 480 and calls[1][1]['image']
    assert cira_bridge.extract_objects({'objects': [obj]}) == [obj]
    assert cira_bridge.send_detections([], 'unused', function='Throttle', min_interval=60) is not None
    assert cira_bridge.send_detections([], 'unused', function='Throttle', min_interval=60) is None
    for bad in [{'DeepDetect': {}}, [{'name': 'x', 'confidence': '1', 'x': 0, 'y': 0, 'w': 1, 'h': 1}], [{'confidence': 1}]]:
        with pytest.raises(ValueError):
            cira_bridge.send_detections(bad, 'unused', min_interval=0)


def test_simulator_frames_are_valid(client):
    import random
    import pytest
    from integrations import simulate_cira
    labels = simulate_cira.labels_from_settings(bh.DEFAULT_SETTINGS)
    assert 'phone' not in labels
    with pytest.raises(ValueError):
        simulate_cira.labels_from_settings(dict(bh.DEFAULT_SETTINGS, label_map={'person': 'person'}))
    rng = random.Random(1)
    people = [{'box': box, 'state': 'attentive', 'student_id': None} for box in simulate_cira.seat_boxes(24)]
    for _ in range(200):
        payload = simulate_cira.make_frame(rng, people, labels)
        assert payload['source'] == 'simulator' and len(payload['detections']) == 24
        for o in payload['detections']:
            assert o['name'] in labels.values()
            assert 0 <= o['x'] and o['x'] + o['w'] <= 1280 and 0 <= o['y'] and o['y'] + o['h'] <= 720
    assert {p['state'] for p in people} - {'attentive', 'inattentive', 'sleeping'} == set()
    start_sim(client)
    assert client.post('/api/cira/frame', json=payload).status_code == 200
