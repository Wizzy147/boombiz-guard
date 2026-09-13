import logging
import sys

import pytest

from app.database.db import Database
from app.security.redact import RedactingFilter, redact
from app.security.vault import CredentialVault, _InsecureDevCipher


@pytest.mark.parametrize(
    "raw",
    [
        "rtsp://admin:S3cret!@192.168.1.64:554/Streaming/Channels/102",
        "open failed for rtsp://admin:S3cret%21@10.0.0.2/cam/realmonitor?channel=1",
        "Authorization: Digest username=\"admin\", response=\"abcd\"",
        'password=S3cret! next',
        '{"password": "S3cret!"}',
        "<wsse:Password Type='x'>S3cret!</wsse:Password>",
    ],
)
def test_redact_masks_secrets(raw):
    out = redact(raw)
    assert "S3cret" not in out
    assert "response=\"abcd\"" not in out


def test_redact_keeps_harmless_text():
    assert redact("rtsp://192.168.1.64:554/Streaming/Channels/102") == "rtsp://192.168.1.64:554/Streaming/Channels/102"


def test_log_filter_redacts_formatted_args(caplog):
    logger = logging.getLogger("t.redact")
    handler = logging.Handler()
    records = []
    handler.emit = records.append
    handler.addFilter(RedactingFilter())
    logger.addHandler(handler)
    logger.warning("opening %s", "rtsp://admin:hunter2@1.2.3.4/x")
    assert "hunter2" not in records[0].getMessage()


def test_vault_roundtrip_insecure_cipher():
    from app.database.models import Device

    db = Database(":memory:")
    with db.session() as s:
        s.add(Device(id="dev_1", ip_address="10.0.0.2"))
    vault = CredentialVault(db, _InsecureDevCipher())
    vault.save_credentials("dev_1", "admin", "p@ss")
    assert vault.get_credentials("dev_1") == ("admin", "p@ss")
    assert "p@ss" not in db.get_secret("dev_1")
    vault.delete_credentials("dev_1")
    assert vault.get_credentials("dev_1") is None


@pytest.mark.skipif(sys.platform != "win32", reason="DPAPI is Windows-only")
def test_vault_dpapi_roundtrip():
    from app.security.vault import dpapi_protect, dpapi_unprotect

    blob = dpapi_protect(b"admin:secret")
    assert b"secret" not in blob
    assert dpapi_unprotect(blob) == b"admin:secret"
