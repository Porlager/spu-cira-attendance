import pytest
import behavior as bh


def settings(**changes):
    return bh.validate_settings(changes)


def det(name, x=0, y=0, w=10, h=10, confidence=0.9, **extra):
    return dict(name=name, confidence=confidence, x=x, y=y, w=w, h=h, **extra)


def test_validate_settings():
    s = settings()
    assert s == bh.DEFAULT_SETTINGS and s["label_map"] is not bh.DEFAULT_SETTINGS["label_map"]
    assert settings(label_map={" Sleep ": "sleeping"})["label_map"] == {"sleep": "sleeping"}
    for bad in [{"label_map": {"sleep": "dead"}}, {"label_map": {"": "sleeping"}}, {"label_map": []},
                {"box_origin": "corner"}, {"coord_units": "inch"}, {"confidence_scale": 10},
                {"confidence_scale": True}, {"min_confidence": 2}, {"min_confidence": float("nan")},
                {"smoothing_frames": 0}, {"smoothing_frames": 11}, {"phone_enabled": 1}, {"other": 1}]:
        with pytest.raises(ValueError):
            bh.validate_settings(bad)
    with pytest.raises(ValueError):
        bh.validate_settings([])
    merged = bh.validate_settings({"min_confidence": 0.3}, base=settings(box_origin="center"))
    assert merged["box_origin"] == "center" and merged["min_confidence"] == 0.3


def test_parse_box_formats():
    assert bh.DEFAULT_SETTINGS["box_origin"] == "center"  # CiRA DeepDetect sends box centers
    dets, _ = bh.parse_detections([det("sleep", 200, 100, 200, 100)], settings(), 400, 200)
    assert dets[0]["box"] == [0.25, 0.25, 0.75, 0.75]
    topleft = settings(box_origin="topleft")
    dets, _ = bh.parse_detections([det("sleep", 100, 50, 200, 100)], topleft, 400, 200)
    assert dets[0]["box"] == [0.25, 0.25, 0.75, 0.75]
    dets, _ = bh.parse_detections([det("sleep", .25, .25, .5, .5)], settings(box_origin="topleft", coord_units="normalized"))
    assert dets[0]["box"] == [0.25, 0.25, 0.75, 0.75]
    dets, _ = bh.parse_detections([det("sleep", 350, 150, 100, 100)], topleft, 400, 200)
    assert dets[0]["box"] == [0.875, 0.75, 1.0, 1.0]
    assert bh.parse_detections([det("sleep", 500, 500)], topleft, 400, 200)[0] == []
    with pytest.raises(ValueError):
        bh.parse_detections([det("sleep")], settings())


def test_unwrap_cira_payload():
    raw = {"DeepDetect": {"count": 1, "im_w": 1100, "im_h": 825, "objects": [det("car")]}}
    assert bh.unwrap_cira_payload(raw) == ("DeepDetect", [det("car")], 1100, 825)
    assert bh.unwrap_cira_payload({"payload": raw})[0] == "DeepDetect"
    assert bh.unwrap_cira_payload({"DeepDetectCPU": {"objects": []}, "other": 1}) == ("DeepDetectCPU", [], None, None)
    two = {"A": {"objects": []}, "B": {"objects": []}}
    assert bh.unwrap_cira_payload(two) is None and bh.unwrap_cira_payload(two, "B")[0] == "B"
    assert bh.unwrap_cira_payload({"x": 1}) is None


def test_parse_validation_and_labels():
    s = settings()
    for bad in [det("sleep", w=0), det("sleep", x=float("nan")), det("sleep", x="1"), det("", ), {"name": "sleep"}, "x"]:
        with pytest.raises(ValueError):
            bh.parse_detections([bad], s, 100, 100)
    with pytest.raises(ValueError):
        bh.parse_detections({}, s, 100, 100)
    with pytest.raises(ValueError):
        bh.parse_detections([det("sleep")] * 301, s, 100, 100)
    dets, unmapped = bh.parse_detections([det("SLEEP"), det("chair"), det("chair"), det("awake", confidence=.2)], s, 100, 100)
    assert [d["behavior"] for d in dets] == ["sleeping"] and unmapped == ["chair"]
    ignored, unmapped = bh.parse_detections([det("chair")], settings(label_map={"chair": "ignore"}), 100, 100)
    assert ignored == [] and unmapped == []


def test_confidence_scale():
    with pytest.raises(ValueError):
        bh.parse_detections([det("sleep", confidence=95)], settings(), 100, 100)
    dets, _ = bh.parse_detections([det("sleep", confidence=90), det("awake", confidence=30)],
                                  settings(confidence_scale=100), 100, 100)
    assert len(dets) == 1 and dets[0]["confidence"] == 0.9


def test_student_id_hint_only_when_allowed():
    with pytest.raises(ValueError):
        bh.parse_detections([det("sleep", student_id="6701")], settings(), 100, 100)
    dets, _ = bh.parse_detections([det("sleep", student_id="6701")], settings(), 100, 100, allow_student_id=True)
    assert bh.match_hints(dets) == {"6701": 0}


def test_summary_and_phone_toggle():
    items = [det("awake")] * 3 + [det("sleep"), det("phone"), det("chair")]
    dets, _ = bh.parse_detections(items, settings(), 100, 100)
    counts = bh.summarize(dets)
    assert counts["people"] == 5 and counts["phone"] == 0 and bh.attentive_pct(counts) == 75.0
    dets, _ = bh.parse_detections(items, settings(phone_enabled=True), 100, 100)
    counts = bh.summarize(dets)
    assert counts["phone"] == 1 and bh.attentive_pct(counts) == 60.0
    both, _ = bh.parse_detections([det("person")] * 4 + [det("awake")] * 2, settings(), 100, 100)
    assert bh.summarize(both)["people"] == 4
    assert bh.attentive_pct(bh.summarize([])) is None


def make(behavior, box, kind="state"):
    return {"behavior": behavior, "kind": kind, "box": box}


def face(student, box):
    return {"student_id": student, "box": box}


def test_match_faces_rules():
    big, small = make("attentive", [0, 0, 1, 1]), make("sleeping", [0, 0, .5, .5])
    assert bh.match_faces([face("a", [.1, .1, .2, .2])], [big, small]) == {"a": 1}
    assert bh.match_faces([face("a", [.6, .6, .7, .7])], [small]) == {}
    assert bh.match_faces([face(None, [.1, .1, .2, .2])], [small]) == {}
    assert bh.match_faces([face("a", [.1, .1, .2, .2]), face("b", [.2, .2, .3, .3])], [small]) == {}
    two = [make("sleeping", [0, 0, .4, .4]), make("attentive", [.5, .5, 1, 1])]
    assert bh.match_faces([face("a", [.1, .1, .2, .2]), face("a", [.6, .6, .7, .7])], two) == {}
    assert bh.match_faces([face("a", [.1, .1, .2, .2])], [make("person", [0, 0, 1, 1], "person")]) == {}


def test_smoother():
    now = [0.0]
    s = bh.Smoother(frames=3, stale_seconds=20, clock=lambda: now[0])
    key = (1, "a")
    assert s.status(key) == ("unknown", 0)
    assert s.update(key, "sleeping") is None
    assert s.update(key, "sleeping") is None
    assert s.update(key, "sleeping") == "sleeping"
    s.update(key, "attentive")
    s.update(key, "attentive")
    assert s.update(key, "sleeping") == "sleeping"
    now[0] = 10
    assert s.status(key) == ("sleeping", 10)
    now[0] = 40
    assert s.status(key)[0] == "not_seen"
    s.update((2, "b"), "attentive")
    s.forget_session(1)
    assert key not in s.state and (2, "b") in s.state
    s.forget_student("b")
    assert s.state == {}
