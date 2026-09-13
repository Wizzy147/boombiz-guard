from app.adapters.base import Compatibility
from app.streams.compat import classify
from app.streams.tester import StreamTestResult


def good(**kw):
    r = StreamTestResult(connected=True, codec="H264", width=640, height=360, nominal_fps=10,
                         measured_fps=9.6, frames=192, seconds_requested=20, seconds_run=20, reconnect_ok=True)
    for k, v in kw.items():
        setattr(r, k, v)
    return r


def test_all_pass_is_compatible():
    rep = classify(authenticated=True, test=good(), has_substream=True, onvif=True, alarm_output=True, audio=True)
    assert rep.status == Compatibility.COMPATIBLE


def test_missing_optional_is_limited():
    rep = classify(authenticated=True, test=good(), has_substream=True, onvif=False, alarm_output=False, audio=False)
    assert rep.status == Compatibility.LIMITED
    assert "ONVIF" in rep.summary


def test_mjpeg_is_incompatible():
    rep = classify(authenticated=True, test=good(codec="MJPEG"), has_substream=True, onvif=True, alarm_output=True, audio=True)
    assert rep.status == Compatibility.INCOMPATIBLE


def test_short_run_is_not_stable():
    rep = classify(authenticated=True, test=good(seconds_run=6), has_substream=True, onvif=True, alarm_output=True, audio=True)
    assert rep.status == Compatibility.INCOMPATIBLE


def test_no_credentials_is_auth_required():
    assert classify(authenticated=False, test=None, has_substream=False, onvif=False, alarm_output=False, audio=False).status == Compatibility.AUTH_REQUIRED
