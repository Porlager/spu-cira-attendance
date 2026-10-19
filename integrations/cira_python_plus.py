"""Code for a CiRA CORE **Python+** node (desktop edition) placed right after DeepDetect.

INPUT : sends this frame's DeepDetect result (+ the same image) to the SPU Classroom web app.
OUTPUT: writes the web app's answer back into the flow as payload["spu"], so later nodes
        (Debug, JavaScript, ...) can use e.g. payload.spu.sleeping or payload.spu.students.

CiRA's Python+ template provides getReqData(req) / makeRespData(payload, img) at runtime;
they are not defined here on purpose. Verified only with fakes, not on a real CiRA machine yet.
Edit the settings below, then paste the whole file into the Python+ node.
"""
import sys

REPO = "/absolute/path/spu-cira-attendance"   # where the repo was cloned on the CiRA machine
BASE_URL = "http://127.0.0.1:5050"            # web app address (same machine only)
ROOM = ""                                     # room code from "ห้องและเซ็ค", e.g. "11-501"; blank = the only open class
SEND_IMAGE = True                             # False: boxes only, no face check-in or per-student result
TIMEOUT = 2                                   # seconds; keeps the flow moving if the web app is down

sys.path.append(REPO + "/integrations")
from cira_bridge import error_message, flow_output, send_detections  # noqa: E402

TOKEN_FILE = REPO + "/data/api-token.txt"
_last = {"status": "waiting"}   # last answer; frames skipped by the 1-per-second throttle reuse it


def handle_request(req):
    global _last
    payload, img = getReqData(req)  # noqa: F821  (provided by CiRA Python+)
    frame = img if SEND_IMAGE and getattr(img, "size", 0) else None
    try:
        result = send_detections(payload, TOKEN_FILE, frame_bgr=frame, base_url=BASE_URL, timeout=TIMEOUT, room=ROOM or None)
        if result is not None:
            _last = dict(flow_output(result), status="ok")
    except Exception as error:  # web app closed, no session open, wrong settings: never stop the flow
        _last = {"status": "error", "error": error_message(error)}
    payload["spu"] = _last
    return makeRespData(payload, img)  # noqa: F821  (provided by CiRA Python+)
