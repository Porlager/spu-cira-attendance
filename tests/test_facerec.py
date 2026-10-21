import sqlite3
import numpy as np
import pytest
import behavior as bh
from integrations import cira_bridge
from test_app import client, enroll  # noqa: F401  (client is a fixture)

SETTINGS = bh.validate_settings({})


def test_parse_faces_and_name_matching():
    faces = bh.parse_faces([{'name': '66060711', 'x': 320, 'y': 240, 'w': 64, 'h': 48, 'distance': 21.9}], SETTINGS, 640, 480)
    assert faces == [{'name': '66060711', 'distance': 21.9, 'box': [0.45, 0.45, 0.55, 0.55]}]
    for bad in [[{'x': 1}], [{'name': 'a', 'x': 'x', 'y': 0, 'w': 1, 'h': 1}], {}]:
        with pytest.raises(ValueError):
            bh.parse_faces(bad, SETTINGS, 640, 480)
    ids = {'66060711', '66060712'}
    assert bh.student_for_name('66060711', ids) == '66060711'
    assert bh.student_for_name('66060711_Lattaphon', ids) == '66060711'
    assert bh.student_for_name('660607111', ids) is None and bh.student_for_name('unknown', ids) is None
    with pytest.raises(ValueError):
        bh.validate_settings({'face_threshold': 5})


def test_bridge_forwards_facerec_and_sends_previews(monkeypatch):
    calls = []
    monkeypatch.setattr(cira_bridge, 'post', lambda *args: calls.append(args) or {'ok': 1})
    monkeypatch.setattr(cira_bridge, '_last_sent', {})
    monkeypatch.setattr(cira_bridge, '_last_preview', {})
    payload = {'DeepDetect': {'im_w': 640, 'im_h': 480, 'objects': [{'name': 'person', 'confidence': .9, 'x': 1, 'y': 2, 'w': 3, 'h': 4}]},
               'FaceRec': {'faces': [{'name': '66060711', 'distance': 21.9, 'rect': [10, 20, 30, 40]}]}, 'spu': {'status': 'ok'}}
    assert cira_bridge.extract_faces(payload) == [{'name': '66060711', 'x': 10, 'y': 20, 'w': 30, 'h': 40, 'distance': 21.9}]
    assert cira_bridge.extract_faces({'DeepDetect': {}}) is None
    assert cira_bridge.extract_faces({'FaceRec': {'faces': []}}) == []
    frame = np.zeros((480, 640, 3), np.uint8)
    assert cira_bridge.send_detections(payload, 'unused', frame_bgr=frame, function='DeepDetect') == {'ok': 1}
    full = calls[0][1]
    assert full['faces'][0]['name'] == '66060711' and 'preview' not in full and full['cira_payload']['FaceRec']
    assert cira_bridge.send_detections(payload, 'unused', frame_bgr=frame, function='DeepDetect', preview_interval=0.0001) is None
    assert calls[1][1]['preview'] is True and 'cira_payload' not in calls[1][1]
    assert cira_bridge.send_detections(payload, 'unused', function='DeepDetect') is None  # no frame: nothing to preview
    assert len(calls) == 2


def face_frame(name, **extra):
    data = {'width': 640, 'height': 480,
            'detections': [{'name': 'sleep', 'confidence': .9, 'x': 320, 'y': 240, 'w': 300, 'h': 300}],
            'faces': [{'name': name, 'x': 320, 'y': 200, 'w': 60, 'h': 60, 'distance': 20}]}
    data.update(extra)
    return data


def test_server_uses_cira_face_names_for_attendance(client, tmp_path):
    client.post('/api/sessions', json={'title': 'CPE447'})
    client.post('/api/students', json=dict(id='67000001', name='A', consent=True, images=['face'] * 3))
    unknown = client.post('/api/cira/frame', json=face_frame('stranger')).json
    assert unknown['faces'][0]['student_id'] is None and client.get('/api/state').json['attendance'] == []
    known = client.post('/api/cira/frame', json=face_frame('67000001_A')).json
    assert known['faces'][0] == {'student_id': '67000001', 'name': '67000001_A', 'distance': 20, 'behavior': 'sleeping'}
    assert client.get('/api/state').json['attendance'][0]['source'] == 'cira-facerec'
    con = sqlite3.connect(tmp_path/'attendance.sqlite3')
    rows = con.execute('SELECT COUNT(*) FROM observations').fetchone()[0]
    preview = client.post('/api/cira/frame', json=face_frame('67000001', preview=True, image='face')).json
    assert preview['preview'] is True and con.execute('SELECT COUNT(*) FROM observations').fetchone()[0] == rows
    lite = client.get('/api/live?lite=1').json
    assert lite['frame']['seq'] == preview['seq'] and lite['frame']['has_image'] and 'roster' not in lite
