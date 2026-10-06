"""Fallback firings skip finished days, stop on uncertainty and share one lock."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from scripts import daily_brief_status_v1 as status
from scripts import send_personal_daily_brief_v3 as v3

DAY = date(2026, 10, 1)
NOW = datetime(2026, 10, 1, 1, 30, tzinfo=UTC)  # 10:30 KST


def _ledger(path: Path, rows: list[tuple[str, str]]) -> Path:
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE daily_issue_reservations (recipient TEXT, "
                           "issue_date TEXT, sha256 TEXT, state TEXT)")
        connection.executemany(
            "INSERT INTO daily_issue_reservations VALUES ('me', '2026-10-01', ?, ?)", rows)
    connection.close()
    return path


def _receipt(root: Path, sha: str) -> None:
    folder = root / "output/newsletter/personal_mail_20261001_v1"
    folder.mkdir(parents=True)
    (folder / "delivery_receipt.json").write_text(
        json.dumps({"state": "SENT", "sha256": sha}), encoding="utf-8")


def test_decisions_follow_the_ledger(tmp_path: Path) -> None:
    (tmp_path / "output/newsletter").mkdir(parents=True)
    empty = tmp_path / "none.sqlite3"
    result = status.status(tmp_path, DAY, NOW, empty)
    assert result["decision"] == "WORK" and result["target_deadline_kst"] == "11:00"
    other = _ledger(tmp_path / "other.sqlite3", [("x" * 64, "SENT")])
    assert status.status(tmp_path, DAY, NOW, other)["decision"] == "RESEND_NEEDED"
    shaky = _ledger(tmp_path / "shaky.sqlite3", [("y" * 64, "UNCERTAIN")])
    assert status.status(tmp_path, DAY, NOW, shaky)["decision"] == "STOP_UNCERTAIN"
    _receipt(tmp_path, "x" * 64)
    assert status.status(tmp_path, DAY, NOW, other)["decision"] == "DONE"


def test_fresh_lock_blocks_a_second_runner_and_stale_lock_is_replaced(tmp_path: Path) -> None:
    (tmp_path / "output/newsletter").mkdir(parents=True)
    assert status.claim(tmp_path, DAY, "09:00", NOW)["claimed"] is True
    assert status.claim(tmp_path, DAY, "10:00", NOW + timedelta(minutes=10))["claimed"] is False
    later = status.claim(tmp_path, DAY, "11:00", NOW + timedelta(minutes=90))
    assert later["claimed"] is True and later["lock"]["replaced"]["runner"] == "09:00"
    assert status.status(tmp_path, DAY, NOW + timedelta(minutes=95),
                         tmp_path / "none.sqlite3")["decision"] == "RUNNING"
    assert status.release(tmp_path, DAY, "11:00")["released"] is True


def test_attachment_name_uses_the_issue_date() -> None:
    assert v3.daily_file_stem("2026-10-01") == "Innergen Carbon Daily_20261001"


def test_evening_slot_and_lock_are_separate_from_morning(tmp_path: Path) -> None:
    (tmp_path / "output/newsletter").mkdir(parents=True)
    evening_now = datetime(2026, 10, 1, 12, 10, tzinfo=UTC)
    ledger = tmp_path / "slots.sqlite3"
    with sqlite3.connect(ledger) as connection:
        connection.execute("CREATE TABLE daily_issue_reservations (recipient TEXT, "
                           "issue_date TEXT, sha256 TEXT, state TEXT)")
        connection.execute("INSERT INTO daily_issue_reservations VALUES "
                           "('me', '2026-10-01', ?, 'SENT')", ("a" * 64,))
    assert status.status(tmp_path, DAY, evening_now, ledger,
                         edition="evening")["decision"] == "WORK"
    assert status.claim(tmp_path, DAY, "21:00", evening_now,
                        edition="evening")["claimed"] is True
    assert status.lock_path(tmp_path, DAY).is_file() is False
    assert status.status(tmp_path, DAY, evening_now, ledger,
                         edition="evening")["decision"] == "RUNNING"
    assert status.release(tmp_path, DAY, "21:00", edition="evening")["released"] is True
    with sqlite3.connect(ledger) as connection:
        connection.execute("INSERT INTO daily_issue_reservations VALUES "
                           "('me', '2026-10-01 21:00', ?, 'SENT')", ("b" * 64,))
    assert status.status(tmp_path, DAY, evening_now, ledger,
                         edition="evening")["decision"] == "DONE"
