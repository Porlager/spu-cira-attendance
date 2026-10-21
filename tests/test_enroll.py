import base64
import numpy as np
import pytest
from vision import Vision, pick_enrollment_face
from test_app import client, enroll  # noqa: F401  (client is a fixture)


def face(w, h=None):
    return ((0, 0, w, h or w), np.zeros((160, 160), np.uint8))


def test_pick_enrollment_face():
    with pytest.raises(ValueError, match='ไม่พบใบหน้า'):
        pick_enrollment_face([])
    assert pick_enrollment_face([face(100)])[0][2] == 100
    assert pick_enrollment_face([face(40), face(120)])[0][2] == 120  # small background detection ignored
    with pytest.raises(ValueError, match='มากกว่า 1 คน'):
        pick_enrollment_face([face(100), face(90)])


def test_face_check_endpoint(client):
    assert client.post('/api/faces/check', json={'image': 'face'}).json['ok'] is True
    bad = client.post('/api/faces/check', json={'image': 'bad'}).json
    assert bad['ok'] is False and bad['reason']


def test_enrollment_skips_unclear_photos_and_names_them(client):
    created = enroll(client, images=['face', 'bad', 'face', 'face'])
    assert created.status_code == 201 and created.json['samples'] == 3 and created.json['skipped'] == [2]
    response = enroll(client, id='67000002', images=['face', 'bad', 'bad'])
    assert response.status_code == 400
    assert 'ใช้ได้ 1 ภาพ' in response.json['error'] and 'ภาพที่ 2' in response.json['error'] and 'ภาพที่ 3' in response.json['error']


def test_real_enroll_faces_on_blank_frame():
    import cv2
    v = Vision()
    ok, image = cv2.imencode('.jpg', np.full((480, 640, 3), 200, np.uint8))
    assert v.enroll_faces(v.decode(base64.b64encode(image).decode())) == []
