"""Code for a CiRA CORE **Python+** node (desktop edition).

Flow: WebCam -> DeepDetect -> FaceRec -> Python+ (this file) -> Debug

INPUT : sends this frame's DeepDetect boxes, FaceRec names and the same image to the SPU Classroom web app.
        Full results go out once a second; lighter picture-only frames in between keep the web view smooth.
OUTPUT: writes the web app's answer back into the flow as payload["spu"], so later nodes
        (Debug, JavaScript, ...) can use e.g. payload.spu.sleeping or payload.spu.students.

CiRA's Python+ template provides getReqData(req) / makeRespData(payload, img) at runtime;
they are not defined here on purpose. Nothing in this file may raise: any problem is reported in
payload["spu"]["error"] (visible in the Debug node) instead of a bare "service call error".
Edit the settings below, then paste the whole file into the Python+ node.
"""
import sys
import traceback

# Keep Python 3.12 standard library and packages ahead of ROS Melodic's Python 2 modules.
ROS_SITE = r"C:\opt\ros\melodic\x64\lib\site-packages"
sys.path[:] = [entry for entry in sys.path if entry.lower() != ROS_SITE.lower()]
sys.path.append(ROS_SITE)

REPO = r"C:\Users\Pound\OneDrive\Desktop\spu-cira-attendance"   # project location on this machine
BASE_URL = "http://127.0.0.1:5050"            # web app address (same machine only)
ROOM = ""                                     # room code from "ห้องและเซ็ค", e.g. "11-501"; blank = the only open class
SEND_IMAGE = True                             # False: boxes only, no picture on the web
TIMEOUT = 2                                   # seconds; keeps the flow moving if the web app is down
PREVIEW_INTERVAL = 0.2                        # seconds between picture-only frames (0 = once a second only)

TOKEN_FILE = REPO + "/data/api-token.txt"
_last = {"status": "waiting"}   # last answer; frames between full sends reuse it

try:
    sys.path.append(REPO + "/integrations")
    from cira_bridge import error_message, flow_output, send_detections  # noqa: E402
    _import_error = None
except Exception:  # wrong REPO path, missing file, ...: report it instead of crashing the service
    _import_error = traceback.format_exc(limit=1).strip().splitlines()[-1]


def handle_request(req):
    global _last
    payload, img = getReqData(req)  # noqa: F821  (provided by CiRA Python+)
    try:
        if _import_error:
            raise RuntimeError("import cira_bridge failed (check REPO): " + _import_error)
        frame = img if SEND_IMAGE and getattr(img, "size", 0) else None
        result = send_detections(payload, TOKEN_FILE, frame_bgr=frame, base_url=BASE_URL, timeout=TIMEOUT,
                                 room=ROOM or None, preview_interval=PREVIEW_INTERVAL)
        if result is not None:
            _last = dict(flow_output(result), status="ok")
    except Exception as error:  # web app closed, no session open, wrong settings: never stop the flow
        try:
            reason = error_message(error)
        except Exception:
            reason = repr(error)
        _last = {"status": "error", "error": reason, "where": traceback.format_exc(limit=2).strip().splitlines()[-1]}
    try:
        payload["spu"] = _last
    except Exception:
        pass
    return makeRespData(payload, img)  # noqa: F821  (provided by CiRA Python+)
