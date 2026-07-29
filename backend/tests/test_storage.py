"""Object storage seam: roundtrip, namespacing, traversal rejection."""

import pytest

from app.services import storage


@pytest.fixture(autouse=True)
def _isolated_storage(tmp_path, monkeypatch):
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "STORAGE_DIR", str(tmp_path))


def test_roundtrip():
    key = storage.put("demo/audits/report.csv", b"po,paid,contracted\n")
    assert key == "demo/audits/report.csv"
    assert storage.exists(key)
    assert storage.get(key) == b"po,paid,contracted\n"


def test_missing_key():
    assert not storage.exists("demo/nope.bin")
    with pytest.raises(FileNotFoundError):
        storage.get("demo/nope.bin")


def test_traversal_rejected():
    with pytest.raises(ValueError):
        storage.put("../outside.txt", b"x")
    with pytest.raises(ValueError):
        storage.get("demo/../../secrets")
