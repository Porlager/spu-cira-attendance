import base64
import io
import csv
import numpy as np
import pytest
from app import create_app
from vision import Vision
from integrations import cira_bridge


class FakeVision:
    def train(self, rows):
        self.rows = rows

    def decode(self, image):
        if image != 'face':
            raise ValueError('invalid image')
        return np.zeros((240, 320, 3), dtype=np.uint8)

    def faces(self, image):
        return [((1, 2, 50, 50), np.zeros((160, 160), dtype=np.uint8))]

    def predict(self, face, threshold):
        return (self.rows[0]['id'], 10) if self.rows else (None, None)


@pytest.fixture
def client(tmp_path):
    app = create_app(tmp_path, FakeVision())
    app.config['TESTING'] = True
    c = app.test_client()
    c.environ_base['HTTP_X_API_KEY'] = (tmp_path/'api-token.txt').read_text()
    return c


def enroll(c, **changes):
    data = dict(id='67000001', name='นักศึกษาทดสอบ', consent=True, images=['face']*3)
    data.update(changes)
    return c.post('/api/students', json=data)


def start(c):
    return c.post('/api/sessions', json={'title': 'CPE447'}).json['id']


def test_home_and_auth(client):
    assert client.get('/').status_code == 200
    assert client.get('/api/state', headers={'X-API-Key': ''}).status_code == 401
    assert client.get('/', headers={'Host': 'evil.example'}).status_code == 403
    assert client.get('/api/state', headers={'Origin': 'https://evil.example'}).status_code == 403


def test_enrollment_validates_and_deduplicates(client):
    assert enroll(client, consent=False).status_code == 400
    assert enroll(client, images=['face']).status_code == 400
    assert enroll(client, id='../oops').status_code == 400
    assert enroll(client, images=['bad']*3).status_code == 400
    assert enroll(client).status_code == 201
    assert enroll(client).status_code == 409
    assert len(client.get('/api/state').json['students']) == 1


def test_attendance_once_per_session_and_reject_closed(client):
    enroll(client)
    sid = start(client)
    assert client.post('/api/sessions', json={'title': 'another'}).status_code == 409
    for _ in range(3):
        assert client.post('/api/recognize', json={'image': 'face', 'session_id': sid}).status_code == 200
    assert len(client.get('/api/state').json['attendance']) == 1
    client.post(f'/api/sessions/{sid}/close')
    assert client.post('/api/recognize', json={'image': 'face', 'session_id': sid}).status_code == 400
    sid2 = start(client)
    assert sid2 != sid
    client.post('/api/recognize', json={'image': 'face', 'session_id': sid2})
    assert len(client.get('/api/state').json['attendance']) == 1


def test_unknown_does_not_check_in(client):
    sid = start(client)
    result = client.post('/api/recognize', json={'image': 'face', 'session_id': sid})
    assert result.json['count'] == 1
    assert result.json['faces'][0]['student_id'] is None
    assert client.get('/api/state').json['attendance'] == []


def test_cira_counts_are_not_attendance(client):
    enroll(client)
    sid = start(client)
    assert client.post('/api/cira/observations', json={'session_id': sid, 'people_count': 12}).status_code == 200
    state = client.get('/api/state').json
    assert state['latest_count']['count'] == 12
    assert state['latest_count']['source'] == 'cira-person'
    assert not state['attendance']
    for value in [-1, True, 1001, '12']:
        assert client.post('/api/cira/observations', json={'session_id': sid, 'people_count': value}).status_code == 400


def test_export_and_deletion(client):
    enroll(client, name='=FORMULA')
    enroll(client, id='67000002', name='Absent')
    sid = start(client)
    client.post('/api/recognize', json={'image': 'face', 'session_id': sid})
    response = client.get(f'/api/sessions/{sid}/export')
    rows = list(csv.reader(io.StringIO(response.data.decode('utf-8-sig'))))
    assert rows[1][1] == "'=FORMULA"
    assert rows[1][2] == 'มาเรียน'
    assert rows[2][2] == 'ไม่พบการเช็คชื่อ'
    assert client.get('/api/sessions/999/export').status_code == 404
    assert client.delete('/api/students/67000001').json['deleted']
    state = client.get('/api/state').json
    assert len(state['students']) == 1
    assert not state['attendance']


def test_persistence(tmp_path):
    c = create_app(tmp_path, FakeVision()).test_client()
    c.environ_base['HTTP_X_API_KEY'] = (tmp_path/'api-token.txt').read_text()
    enroll(c)
    sid = start(c)
    c.post('/api/recognize', json={'image': 'face', 'session_id': sid})
    second = create_app(tmp_path, FakeVision()).test_client()
    second.environ_base['HTTP_X_API_KEY'] = c.environ_base['HTTP_X_API_KEY']
    assert len(second.get('/api/state').json['attendance']) == 1


def test_real_vision_blank_and_invalid():
    import cv2
    v = Vision()
    with pytest.raises(ValueError):
        v.decode('not image')
    ok, image = cv2.imencode('.jpg', np.zeros((480,640,3), dtype=np.uint8))
    assert ok
    assert v.faces(v.decode(base64.b64encode(image).decode())) == []


def test_real_lbph_train_predict():
    v = Vision()
    sample = np.random.default_rng(42).integers(0, 256, (160,160), dtype=np.uint8)
    v.train([{'id': 'synthetic', 'samples': [sample.tobytes()]}])
    identity, distance = v.predict(sample, 55)
    assert identity == 'synthetic' and distance == 0
    v.train([])
    assert v.predict(sample, 55) == (None, None)


def test_cira_adapter_filters_and_validates(monkeypatch):
    calls = []
    monkeypatch.setattr(cira_bridge, 'post', lambda *args: calls.append(args))
    cira_bridge.send_deepdetect({'DeepDetect': {'objects': [
        {'name': 'person', 'confidence': .9}, {'name': 'person', 'confidence': .1},
        {'name': 'chair', 'confidence': .99}]}}, 1, 'unused')
    assert calls[0][1] == {'session_id': 1, 'people_count': 1}
    with pytest.raises(ValueError):
        cira_bridge.send_deepdetect({}, 1, 'unused')
    with pytest.raises(ValueError):
        cira_bridge.send_deepdetect({'DeepDetect': {'objects': [{'name': 'person', 'confidence': 95}]}}, 1, 'unused')
