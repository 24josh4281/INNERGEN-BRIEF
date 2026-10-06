"""Tell a scheduled daily-brief run whether to work, skip or stop, and hold a run lock.

Design Ref: operator request 2026-09-30 — deliver by 10:00, else by 11:00, 13:00 and
15:00. The task fires at 09:00, 10:00, 11:00 and 13:00; each firing calls this first.
Decisions:
- DONE: this program's brief for the date was accepted by SMTP;
- STOP_UNCERTAIN: an attempt has no confirmed outcome (check the mailbox, never retry);
- RUNNING: another run holds a fresh lock;
- RESEND_NEEDED: the date's daily slot was taken by another sender (e.g. the Codex
  morning mail), so this brief goes out once through the explicit resend path;
- WORK: nothing sent yet.
Read-only on the delivery log; the lock lives in the day's output folder.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import send_personal_daily_brief_v2 as v2  # noqa: E402

KST = ZoneInfo("Asia/Seoul")
LOCK_TTL = timedelta(minutes=55)
DEADLINES = ("10:00", "11:00", "13:00", "15:00")


def _receipts(root: Path, day: date, edition: str = "morning") -> list[dict[str, Any]]:
    found = []
    pattern = (f"personal_mail_evening_{day:%Y%m%d}_*/delivery_receipt.json"
               if edition == "evening" else
               f"personal_mail_{day:%Y%m%d}_*/delivery_receipt.json")
    for path in sorted((root / "output/newsletter").glob(
            pattern)):
        try:
            found.append({**json.loads(path.read_text(encoding="utf-8")), "path": str(path)})
        except (OSError, ValueError):
            continue
    return found


def _ledger(log: Path, day: date, edition: str = "morning") -> dict[str, Any]:
    if not log.is_file():
        return {"daily": None, "resends": []}
    connection = sqlite3.connect(f"file:{log}?mode=ro", uri=True)
    try:
        daily = connection.execute(
            "SELECT sha256, state FROM daily_issue_reservations WHERE issue_date = ?",
            (f"{day.isoformat()} 21:00" if edition == "evening"
             else day.isoformat(),)).fetchone()
        tables = {row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        resends = list(connection.execute(
            "SELECT sha256, state FROM personal_resend_reservations WHERE issue_date = ?",
            (day.isoformat(),))) if (edition == "morning" and
                                     "personal_resend_reservations" in tables) else []
    finally:
        connection.close()
    return {"daily": daily, "resends": resends}


def lock_path(root: Path, day: date, edition: str = "morning") -> Path:
    suffix = "_evening" if edition == "evening" else ""
    return root / f"output/newsletter/daily_brief_{day:%Y%m%d}{suffix}/run.lock"


def _lock(root: Path, day: date, now: datetime,
          edition: str = "morning") -> dict[str, Any] | None:
    path = lock_path(root, day, edition)
    if not path.is_file():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    claimed = datetime.fromisoformat(data["claimed_at_utc"])
    return {**data, "fresh": now - claimed < LOCK_TTL}


def status(root: Path, day: date, now: datetime, log: Path | None = None,
           edition: str = "morning") -> dict[str, Any]:
    ledger_path = log or v2.resolve_delivery_log()
    ledger = _ledger(ledger_path, day, edition)
    receipts = [r for r in _receipts(root, day, edition) if r.get("state") == "SENT"]
    ours = {r.get("sha256") for r in receipts}
    states = []
    if ledger["daily"]:
        states.append(("daily", *ledger["daily"]))
    states.extend(("resend", sha, state) for sha, state in ledger["resends"])
    lock = _lock(root, day, now, edition)
    local = now.astimezone(KST)
    deadline = ("23:30" if local.strftime("%H:%M") < "23:30" else None) if (
        edition == "evening") else next(
            (d for d in DEADLINES if local.strftime("%H:%M") < d), None)
    if ((edition == "evening" and ledger["daily"] and ledger["daily"][1] == "SENT")
            or any(sha in ours and state == "SENT" for _, sha, state in states)):
        decision = "DONE"
    elif any(state in {"ATTEMPTING", "UNCERTAIN"} for _, _, state in states):
        decision = "STOP_UNCERTAIN"
    elif lock and lock["fresh"]:
        decision = "RUNNING"
    elif edition == "morning" and ledger["daily"] and ledger["daily"][1] == "SENT":
        decision = "RESEND_NEEDED"
    else:
        decision = "WORK"
    folder = root / f"output/newsletter/daily_brief_{day:%Y%m%d}"
    if edition == "evening":
        folder = folder.with_name(folder.name + "_evening")
    return {"date": day.isoformat(), "edition": edition,
            "now_kst": local.isoformat(timespec="minutes"),
            "decision": decision, "target_deadline_kst": deadline,
            "delivery_log": str(ledger_path),
            "ledger": [{"kind": k, "sha256": s, "state": st} for k, s, st in states],
            "our_receipts": [r["path"] for r in receipts], "lock": lock,
            "artifacts": sorted(p.name for p in folder.glob("*")
                                if p.is_file()) if folder.is_dir() else []}


def claim(root: Path, day: date, runner: str, now: datetime,
          edition: str = "morning") -> dict[str, Any]:
    """Take the run lock unless a fresh one exists; a stale lock is replaced and noted."""
    path = lock_path(root, day, edition)
    path.parent.mkdir(parents=True, exist_ok=True)
    current = _lock(root, day, now, edition)
    if current and current["fresh"]:
        return {"claimed": False, "lock": current}
    record = {"runner": runner, "claimed_at_utc": now.astimezone(UTC).isoformat(),
              "replaced": current}
    temp = path.with_suffix(f".{os.getpid()}.tmp")
    temp.write_text(json.dumps(record, ensure_ascii=False) + "\n", encoding="utf-8")
    temp.replace(path)
    return {"claimed": True, "lock": record}


def release(root: Path, day: date, runner: str,
            edition: str = "morning") -> dict[str, Any]:
    path = lock_path(root, day, edition)
    if path.is_file() and json.loads(path.read_text(encoding="utf-8")).get("runner") == runner:
        done = path.with_name(f"run.lock.done.{datetime.now(UTC):%H%M%S}")
        path.replace(done)
        return {"released": True}
    return {"released": False}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", type=date.fromisoformat,
                        default=datetime.now(KST).date())
    parser.add_argument("--edition", choices=("morning", "evening"), default="morning")
    parser.add_argument("--claim", metavar="RUNNER")
    parser.add_argument("--release", metavar="RUNNER")
    args = parser.parse_args()
    now = datetime.now(UTC)
    if args.claim:
        result = claim(ROOT, args.date, args.claim, now, args.edition)
    elif args.release:
        result = release(ROOT, args.date, args.release, args.edition)
    else:
        result = status(ROOT, args.date, now, edition=args.edition)
    print(json.dumps(result, ensure_ascii=False, indent=1, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
