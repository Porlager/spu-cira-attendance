import base64
import re
import pytest
from app import create_app
from test_app import FakeVision


@pytest.fixture
def app(tmp_path):
    app = create_app(tmp_path, FakeVision())
    app.config.update(TESTING=True, PASSWORD_METHOD='pbkdf2:sha256:1000')  # fast hashes in tests
    return app


@pytest.fixture
def machine(app, tmp_path):
    c = app.test_client()
    c.environ_base['HTTP_X_API_KEY'] = (tmp_path/'api-token.txt').read_text()
    return c


def browser(app, username, password):
    """A cookie-based client logged in through the form, with the CSRF header set like common.js does."""
    c = app.test_client()
    response = c.post('/login', data={'username': username, 'password': password, 'next': '/'})
    assert response.status_code == 302, response.data
    html = c.get('/').data.decode()
    c.environ_base['HTTP_X_CSRF_TOKEN'] = re.search(r'name="csrf-token" content="([^"]+)"', html).group(1)
    return c


def user(machine, username, role='teacher'):
    return machine.post('/api/users', json={'username': username, 'name': username.title(), 'role': role,
                                            'password': 'password-123'}).json['id']


def room(machine, code):
    return machine.post('/api/rooms', json={'code': code}).json['id']


def section(machine, room_id, code, teacher_id=None, sec='01'):
    return machine.post('/api/sections', json={'room_id': room_id, 'course_code': code, 'section': sec,
                                               'teacher_id': teacher_id}).json['id']


def test_first_run_setup_login_and_logout(app):
    c = app.test_client()
    assert c.get('/').headers['Location'].endswith('/setup')
    assert c.get('/login').headers['Location'].endswith('/setup')
    assert c.post('/setup', data={'username': 'admin', 'name': 'Admin', 'password': 'password-1', 'confirm': 'other'}).status_code == 400
    assert c.post('/setup', data={'username': 'admin', 'name': 'Admin', 'password': 'short', 'confirm': 'short'}).status_code == 400
    assert c.post('/setup', data={'username': 'a!', 'name': 'Admin', 'password': 'password-1', 'confirm': 'password-1'}).status_code == 400
    response = c.post('/setup', data={'username': 'admin', 'name': 'Admin', 'password': 'password-1', 'confirm': 'password-1'})
    assert response.headers['Location'].endswith('/admin/sections')
    assert c.get('/setup').headers['Location'].endswith('/login')
    html = c.get('/admin/sections').data.decode()
    token = re.search(r'name="csrf-token" content="([^"]+)"', html).group(1)
    assert c.get('/api/me').status_code == 403  # cookie alone is not enough
    assert c.get('/api/me', headers={'X-CSRF-Token': token}).json['user']['role'] == 'admin'
    c.post('/logout', data={'csrf': token})
    assert c.get('/api/me', headers={'X-CSRF-Token': token}).status_code == 401
    assert c.get('/live?session=3').headers['Location'] == '/login?next=/live%3Fsession%3D3'


def test_login_failures_and_safe_redirect(app, machine):
    user(machine, 'teacher1')
    c = app.test_client()
    assert c.post('/login', data={'username': 'teacher1', 'password': 'nope'}).status_code == 401
    assert c.post('/login', data={'username': 'teacher1', 'password': 'password-123', 'next': '//evil.example'}).headers['Location'] == '/'
    for _ in range(5):
        c.post('/login', data={'username': 'Teacher1', 'password': 'wrong-pass'})
    assert c.post('/login', data={'username': 'teacher1', 'password': 'password-123'}).status_code == 429


def test_teacher_sees_and_runs_only_own_sections(app, machine):
    a, b = user(machine, 'teacher_a'), user(machine, 'teacher_b')
    r1, r2 = room(machine, '11-501'), room(machine, '11-502')
    s1, s2, s3 = section(machine, r1, 'CPE447', a), section(machine, r2, 'CPE448', b), section(machine, r1, 'CPE449', b)
    ta = browser(app, 'teacher_a', 'password-123')
    assert [r['code'] for r in ta.get('/api/rooms').json['rooms']] == ['11-501']
    assert [s['id'] for s in ta.get('/api/sections?room=%d' % r1).json['sections']] == [s1]
    assert ta.get('/api/sections/%d' % s2).status_code == 404
    assert ta.post('/api/sessions', json={'section_id': s2}).status_code == 404
    assert ta.post('/api/sessions', json={'title': 'ad hoc'}).status_code == 400
    for path in ['/api/students', '/api/users', '/api/settings', '/api/cira/outputs', '/api/state']:
        assert ta.get(path).status_code == 403, path
    assert ta.post('/api/sections/%d/import' % s1, json={'csv': '67000001,A'}).status_code == 403
    assert ta.post('/api/cira/frame', json={'detections': []}).status_code == 403
    assert ta.get('/admin/sections').headers['Location'] == '/'
    assert ta.get('/').status_code == 200
    sid = ta.post('/api/sessions', json={'section_id': s1}).json['id']
    tb = browser(app, 'teacher_b', 'password-123')
    assert tb.post('/api/sessions', json={'section_id': s3}).status_code == 409  # room 11-501 is busy
    assert tb.post('/api/sessions/%d/close' % sid).status_code == 404
    assert tb.get('/api/live?session=%d' % sid).status_code == 404
    assert tb.get('/api/sessions/%d/report' % sid).status_code == 404
    assert [s['id'] for s in tb.get('/api/sessions').json['sessions']] == []
    assert ta.get('/api/live').json['session']['id'] == sid
    assert ta.post('/api/sessions/%d/close' % sid).json['closed']


def test_one_open_class_per_room_and_cira_room_routing(machine):
    r1, r2 = room(machine, '11-501'), room(machine, '11-502')
    s1, s2 = section(machine, r1, 'CPE447'), section(machine, r2, 'CPE448')
    id1 = machine.post('/api/sessions', json={'section_id': s1}).json['id']
    assert machine.post('/api/sessions', json={'section_id': s1}).status_code == 409
    id2 = machine.post('/api/sessions', json={'section_id': s2}).json['id']
    frame = {'detections': [{'name': 'sleep', 'confidence': .9, 'x': 50, 'y': 50, 'w': 20, 'h': 20}], 'width': 100, 'height': 100}
    response = machine.post('/api/cira/frame', json=frame)
    assert response.status_code == 400 and 'room' in response.json['error']
    assert machine.post('/api/cira/frame', json=dict(frame, room='11-502')).json['session_id'] == id2
    assert machine.post('/api/cira/frame', json=dict(frame, room='9-999')).status_code == 400
    assert machine.get('/api/live?session=%d' % id2).json['frame'] is not None
    assert machine.get('/api/live?session=%d' % id1).json['frame'] is None
    assert machine.get('/api/live/frame?session=%d' % id1).status_code == 404
    machine.post('/api/sessions/%d/close' % id1)
    assert machine.post('/api/cira/frame', json=frame).json['session_id'] == id2  # only one open now


def test_roster_limits_attendance_to_the_section(machine):
    machine.post('/api/students', json=dict(id='67000001', name='A', consent=True, images=['face'] * 3))
    r = room(machine, '11-501')
    s = section(machine, r, 'CPE447')
    machine.post('/api/sections/%d/import' % s, json={'csv': '67000002,B'})
    sid = machine.post('/api/sessions', json={'section_id': s}).json['id']
    machine.post('/api/recognize', json={'image': 'face', 'session_id': sid})  # FakeVision always sees 67000001
    live = machine.get('/api/live?session=%d' % sid).json
    assert [r['id'] for r in live['roster']] == ['67000002'] and live['kpi']['present'] == 0
    assert live['roster'][0]['has_face'] is False and live['kpi']['with_faces'] == 0
    machine.post('/api/sections/%d/students' % s, json={'student_ids': ['67000001']})
    machine.post('/api/recognize', json={'image': 'face', 'session_id': sid})
    assert machine.get('/api/live?session=%d' % sid).json['kpi']['present'] == 1
    report = machine.get('/api/sessions/%d/report' % sid).json
    assert [s['id'] for s in report['students']] == ['67000001', '67000002'] and report['session']['room_code'] == '11-501'


def test_csv_import_preview_and_commit(machine):
    s = section(machine, room(machine, '11-501'), 'CPE447')
    machine.post('/api/students', json=dict(id='67000001', name='เดิม', consent=True, images=['face'] * 3))
    text = 'รหัส,ชื่อ,นามสกุล\n67000001,ใหม่,ชื่อ\n67000002,สมหญิง,รักเรียน\nbad!,x,y\n'
    file = base64.b64encode(text.encode('cp874')).decode()
    preview = machine.post('/api/sections/%d/import' % s, json={'file': file, 'dry_run': True}).json
    assert preview['added'] == 2 and preview['created'] == 1 and len(preview['errors']) == 1 and preview['dry_run']
    assert preview['name_conflicts'] == [{'id': '67000001', 'current': 'เดิม', 'csv': 'ใหม่ ชื่อ'}]
    assert machine.get('/api/sections/%d' % s).json['students'] == []
    done = machine.post('/api/sections/%d/import' % s, json={'file': file}).json
    assert done['added'] == 2 and done['created'] == 1
    students = machine.get('/api/sections/%d' % s).json['students']
    assert [(x['id'], x['name'], x['samples']) for x in students] == [('67000001', 'เดิม', 3), ('67000002', 'สมหญิง รักเรียน', 0)]
    again = machine.post('/api/sections/%d/import' % s, json={'file': file}).json
    assert again['added'] == 0 and again['already_in_section'] == 2
    assert machine.post('/api/sections/%d/import' % s, json={'file': 'not base64!'}).status_code == 400
    assert machine.post('/api/sections/%d/import' % s, json={}).status_code == 400
    listed = {x['id']: x for x in machine.get('/api/students').json['students']}
    assert listed['67000002']['sections'] == 'CPE447/01' and listed['67000002']['samples'] == 0


def test_faces_for_imported_student(machine):
    s = section(machine, room(machine, '11-501'), 'CPE447')
    machine.post('/api/sections/%d/import' % s, json={'csv': '67000009,จากไฟล์'})
    assert machine.post('/api/students/67000009/faces', json={'consent': False, 'images': ['face'] * 3}).status_code == 400
    assert machine.post('/api/students/nobody/faces', json={'consent': True, 'images': ['face'] * 3}).status_code == 404
    assert machine.post('/api/students/67000009/faces', json={'consent': True, 'images': ['face'] * 3}).json['samples'] == 3
    assert machine.get('/api/sections/%d' % s).json['section']['with_faces'] == 1


def test_rooms_sections_and_users_rules(app, machine):
    admin = user(machine, 'boss', 'admin')
    r = room(machine, '11-501')
    assert machine.post('/api/rooms', json={'code': '11-501'}).status_code == 409
    assert machine.post('/api/rooms', json={'code': 'bad/code'}).status_code == 400
    s = section(machine, r, 'CPE447')
    assert machine.post('/api/sections', json={'room_id': r, 'course_code': 'CPE447', 'section': '01'}).status_code == 409
    assert machine.post('/api/sections', json={'room_id': 999, 'course_code': 'X', 'section': '1'}).status_code == 400
    assert machine.delete('/api/rooms/%d' % r).status_code == 409
    machine.post('/api/sessions', json={'section_id': s})
    assert machine.delete('/api/sections/%d' % s).status_code == 409
    assert machine.patch('/api/sections/%d' % s, json={'teacher_id': admin, 'course_name': 'AI'}).status_code == 200
    assert machine.get('/api/sections/%d' % s).json['section']['teacher_name'] == 'Boss'
    assert machine.post('/api/users', json={'username': 'boss', 'name': 'x', 'role': 'teacher', 'password': 'password-123'}).status_code == 409
    assert machine.post('/api/users', json={'username': 'x', 'name': 'x', 'role': 'teacher', 'password': 'password-123'}).status_code == 400
    assert machine.patch('/api/users/%d' % admin, json={'role': 'teacher'}).status_code == 400  # last admin
    c = browser(app, 'boss', 'password-123')
    assert c.delete('/api/users/%d' % admin).status_code == 400  # not yourself
    assert c.post('/api/me/password', json={'current': 'wrong', 'new': 'new-password-1'}).status_code == 400
    assert c.post('/api/me/password', json={'current': 'password-123', 'new': 'new-password-1'}).json['saved']
    browser(app, 'boss', 'new-password-1')


def test_bridge_sends_room(monkeypatch):
    from integrations import cira_bridge
    calls = []
    monkeypatch.setattr(cira_bridge, 'post', lambda *args: calls.append(args) or {})
    cira_bridge.send_detections([], 'unused', width=10, height=10, function='Room', min_interval=0, room='11-501')
    assert calls[0][1]['room'] == '11-501'
