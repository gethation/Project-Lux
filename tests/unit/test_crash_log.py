from __future__ import annotations

from datetime import datetime

from lux_trader.crash_log import crash_log_path, record_crash


def _boom() -> None:
    raise RuntimeError("order_id_collision: 2 already has different data")


def test_records_the_full_traceback_not_just_the_message(tmp_path) -> None:
    # The message alone is what reporter.error() and SystemExit already carry,
    # and on 2026-08-07 that was all that survived the crash. The file has to
    # add the part that was missing: where it happened.
    try:
        _boom()
    except RuntimeError as exc:
        path = record_crash(exc, context="live-execute", log_dir=tmp_path)

    assert path is not None
    text = path.read_text(encoding="utf-8")
    assert "order_id_collision" in text
    assert "Traceback (most recent call last)" in text
    assert "_boom" in text
    assert "context=live-execute" in text


def test_appends_rather_than_overwrites(tmp_path) -> None:
    # Two crashes in one session must both survive; the second must not erase
    # the first, which is usually the more informative one.
    for marker in ("first failure", "second failure"):
        try:
            raise ValueError(marker)
        except ValueError as exc:
            record_crash(exc, context="live-execute", log_dir=tmp_path)

    text = crash_log_path(log_dir=tmp_path).read_text(encoding="utf-8")
    assert "first failure" in text
    assert "second failure" in text


def test_creates_the_log_directory_when_absent(tmp_path) -> None:
    target = tmp_path / "does" / "not" / "exist"
    try:
        _boom()
    except RuntimeError as exc:
        path = record_crash(exc, context="live-execute", log_dir=target)
    assert path is not None and path.exists()


def test_never_raises_even_when_the_path_is_unusable(tmp_path) -> None:
    # A crash logger that throws would mask the failure it exists to explain,
    # so an unwritable destination must return None rather than propagate.
    blocker = tmp_path / "blocked"
    blocker.write_text("not a directory", encoding="utf-8")
    try:
        _boom()
    except RuntimeError as exc:
        assert record_crash(exc, context="live-execute", log_dir=blocker) is None


def test_filename_carries_the_date(tmp_path) -> None:
    when = datetime(2026, 8, 7, 21, 53).astimezone()
    assert crash_log_path(when, tmp_path).name == "lux_error.20260807.log"
