from __future__ import annotations

import io
import zipfile
from datetime import date

import pytest

from garmin_health_gateway.provider import archive_fit, extract_fit_bytes


def _zip(files: dict[str, bytes]) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        for name, payload in files.items():
            archive.writestr(name, payload)
    return output.getvalue()


def test_extract_fit_chooses_largest_fit_member() -> None:
    payload = _zip({"small.fit": b"a", "activity.fit": b"real-fit", "note.txt": b"x"})
    assert extract_fit_bytes(payload) == b"real-fit"


def test_extract_fit_rejects_zip_without_fit() -> None:
    with pytest.raises(ValueError, match="no FIT"):
        extract_fit_bytes(_zip({"readme.txt": b"nothing"}))


def test_archive_fit_is_idempotent(tmp_path) -> None:
    first = archive_fit(b"first", tmp_path, 42, date(2026, 8, 28))
    second = archive_fit(b"second", tmp_path, 42, date(2026, 8, 28))
    assert first == second
    assert first.read_bytes() == b"first"
    assert first.name == "42.fit"
