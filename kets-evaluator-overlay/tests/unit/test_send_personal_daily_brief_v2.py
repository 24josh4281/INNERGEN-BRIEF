"""The v2 personal edition mirrors section pages and resends only on request."""

from __future__ import annotations

import hashlib
import json
import os
import smtplib
import sqlite3
from datetime import UTC, datetime
from email import policy
from email.parser import BytesParser
from pathlib import Path
from typing import Any, ClassVar

import pytest
from scripts import send_personal_daily_brief_v2 as personal

from kets_intelligence.application.smtp_delivery import ACCOUNT, DeliveryError

NOW = datetime(2026, 9, 29, 9, 40, tzinfo=UTC)
SUMMARY_URL = "https://example.org/summary"
LINK_URL = "https://example.org/link"


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _one_page_pdf() -> bytes:
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 200] /Contents 4 0 R >>",
        b"<< /Length 0 >>\nstream\n\nendstream",
    ]
    raw = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for index, obj in enumerate(objects, start=1):
        offsets.append(len(raw))
        raw.extend(f"{index} 0 obj\n".encode())
        raw.extend(obj + b"\nendobj\n")
    start = len(raw)
    raw.extend(f"xref\n0 {len(offsets)}\n0000000000 65535 f \n".encode())
    for offset in offsets[1:]:
        raw.extend(f"{offset:010d} 00000 n \n".encode())
    raw.extend(
        f"trailer\n<< /Size {len(offsets)} /Root 1 0 R >>\n"
        f"startxref\n{start}\n%%EOF\n".encode()
    )
    return bytes(raw)


def _fixture(root: Path) -> Path:
    source: dict[str, Any] = {
        "issue_date": "2026-09-29",
        "cutoff_at": "2026-09-29T15:20:00+09:00",
        "articles": [
            {
                "id": "summary", "section": "국내 정책", "headline": "요약한 정책 기사",
                "publisher": "기관", "published_date": "2026-09-28",
                "published_at": "2026-09-28T16:10:30+09:00", "url": SUMMARY_URL,
                "bullets": ["첫 번째 확인 사실임", "두 번째 확인 사실 <표시>임"],
                "emphasis": ["첫 번째", "<표시>"],
                "rights": {"internal_analysis_allowed": True},
            },
            {
                "id": "link", "section": "국내 정책", "headline": "제목만 싣는 기사",
                "publisher": "매체", "published_date": "2026-09-28", "url": LINK_URL,
                "bullets": [], "emphasis": [], "rights": {"internal_analysis_allowed": True},
            },
        ],
        "kau": {
            "instrument": "KAU26", "venue": "KRX", "price_unit": "KRW/tCO2e",
            "volume_unit": "tCO2e",
            "observations": [{
                "trade_date": "2026-09-29", "close_krw_per_tco2e": 30050,
                "volume_tco2e": 109502, "internal_analysis_allowed": True,
            }],
            "buy_pressure": {"summary": "순매수 10t", "internal_analysis_allowed": True},
            "sell_pressure": {"summary": "순매도 10t", "internal_analysis_allowed": True},
        },
    }
    source_path = root / "content.json"
    source_path.write_text(json.dumps(source, ensure_ascii=False), encoding="utf-8")
    md_path = root / "reader.md"
    md_path.write_text(
        f"# 브리프\n\n[원문 기사 확인]({SUMMARY_URL})\n- [링크]({LINK_URL})\n", encoding="utf-8"
    )
    pdf_path = root / "reader.pdf"
    pdf_path.write_bytes(_one_page_pdf())
    manifest = {
        "status": "internal_self_review", "customer_distribution_approved": False,
        "sent": False, "layout": personal.LAYOUT, "issue_date": "2026-09-29", "pdf_pages": 1,
        "input_path": str(source_path), "input_sha256": _sha(source_path.read_bytes()),
        "pdf": str(pdf_path), "pdf_sha256": _sha(pdf_path.read_bytes()),
        "markdown": str(md_path), "markdown_sha256": _sha(md_path.read_bytes()),
        "included_article_ids": ["summary", "link"], "main_article_ids": ["summary"],
        "additional_headline_ids": ["link"], "section_counts": {"국내 정책": 2},
    }
    manifest_path = root / "brief_manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    return manifest_path


def _evening_fixture(root: Path) -> Path:
    manifest_path = _fixture(root)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    content = root / "content.json"
    source = json.loads(content.read_text(encoding="utf-8"))
    source["cutoff_at"] = "2026-09-29T21:00:00+09:00"
    source["window_start"] = "2026-09-29T09:00:00+09:00"
    for article in source["articles"]:
        article["published_date"] = "2026-09-29"
        article["published_at"] = "2026-09-29T10:00:00+09:00"
    content.write_text(json.dumps(source, ensure_ascii=False), encoding="utf-8")
    manifest["input_sha256"] = _sha(content.read_bytes())
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    return manifest_path


class FakeSMTP:
    calls: ClassVar[list[list[str]]] = []

    def login(self, username: str, password: str) -> None:
        assert username == ACCOUNT and password == "test-only"

    def sendmail(self, sender: str, recipients: list[str], raw: bytes) -> dict[str, str]:
        assert sender == ACCOUNT and b"application/pdf" in raw
        FakeSMTP.calls.append(recipients)
        return {}

    def close(self) -> None:
        pass


@pytest.fixture
def smtp(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    FakeSMTP.calls = []
    monkeypatch.setattr(personal, "load_password", lambda: "test-only")
    monkeypatch.setattr(smtplib, "SMTP_SSL", lambda *args, **kwargs: FakeSMTP())
    return FakeSMTP.calls


def _original_issue(ledger: Path, state: str) -> None:
    with sqlite3.connect(ledger) as connection:
        connection.execute(
            "CREATE TABLE daily_issue_reservations (recipient TEXT NOT NULL, "
            "issue_date TEXT NOT NULL, sha256 TEXT NOT NULL, state TEXT NOT NULL, "
            "PRIMARY KEY (recipient, issue_date))"
        )
        connection.execute(
            "INSERT INTO daily_issue_reservations VALUES (?, '2026-09-29', ?, ?)",
            (ACCOUNT, "b" * 64, state),
        )
    connection.close()


def test_html_puts_summaries_before_title_links(tmp_path: Path) -> None:
    manifest = _fixture(tmp_path)
    output = tmp_path / "mail_v1"
    personal.prepare_personal_email(manifest, output, workspace_root=tmp_path, now=NOW)
    message = BytesParser(policy=policy.default).parsebytes((output / "message.eml").read_bytes())
    assert message["Subject"] == "이너젠 탄소·에너지 일간 브리프 | 2026-09-29"
    assert message.get("X-KETS-Resend") is None
    body = (output / "body.html").read_text(encoding="utf-8")
    assert "MAIN" not in body
    assert body.index("요약한 정책 기사") < body.index("그 밖의 기사") < body.index(
        "제목만 싣는 기사"
    )
    assert "<strong>&lt;표시&gt;</strong>" in body
    assert "2026-09-28 16:10 KST" in body
    assert body.count(SUMMARY_URL) == 1 and body.count(LINK_URL) == 1


def test_first_send_uses_the_daily_issue_claim(tmp_path: Path, smtp: list[list[str]]) -> None:
    output = tmp_path / "mail_v1"
    personal.prepare_personal_email(_fixture(tmp_path), output, workspace_root=tmp_path, now=NOW)
    ledger = tmp_path / "attempts.sqlite3"
    result = personal.send_personal_email(
        output, workspace_root=tmp_path, log_path=ledger, now=NOW
    )
    assert result["state"] == "SENT" and result["resend"] is False
    assert smtp == [[ACCOUNT]]
    with pytest.raises(DeliveryError, match="already sent or attempted"):
        personal.send_personal_email(output, workspace_root=tmp_path, log_path=ledger, now=NOW)
    assert smtp == [[ACCOUNT]]


def test_evening_has_own_subject_and_one_send_slot(
    tmp_path: Path, smtp: list[list[str]],
) -> None:
    now = datetime(2026, 9, 29, 13, 0, tzinfo=UTC)
    ledger = tmp_path / "attempts.sqlite3"
    morning = tmp_path / "morning_mail_v1"
    personal.prepare_personal_email(_fixture(tmp_path), morning,
                                    workspace_root=tmp_path, now=NOW)
    personal.send_personal_email(morning, workspace_root=tmp_path,
                                 log_path=ledger, now=NOW)
    output = tmp_path / "evening_mail_v1"
    personal.prepare_personal_email(_evening_fixture(tmp_path), output,
                                    workspace_root=tmp_path, now=now, edition="evening")
    message = BytesParser(policy=policy.default).parsebytes((output / "message.eml").read_bytes())
    assert message["Subject"].endswith("| 21:00 저녁판")
    assert message["X-KETS-Edition"] == "evening"
    assert message.get("X-KETS-Resend") is None
    result = personal.send_personal_email(output, workspace_root=tmp_path,
                                          log_path=ledger, now=now)
    assert result["state"] == "SENT" and result["resend"] is False
    receipt = json.loads((output / "delivery_receipt.json").read_text(encoding="utf-8"))
    assert receipt["delivery_slot"] == "2026-09-29 21:00"
    assert receipt["edition"] == "evening"
    with sqlite3.connect(ledger) as connection:
        slots = connection.execute("SELECT issue_date FROM daily_issue_reservations").fetchall()
    assert slots == [("2026-09-29",), ("2026-09-29 21:00",)]
    with pytest.raises(DeliveryError, match="already sent or attempted"):
        personal.send_personal_email(output, workspace_root=tmp_path,
                                     log_path=ledger, now=now)
    assert smtp == [[ACCOUNT], [ACCOUNT]]


def test_evening_rejects_morning_article_before_draft(tmp_path: Path) -> None:
    manifest = _evening_fixture(tmp_path)
    content = tmp_path / "content.json"
    source = json.loads(content.read_text(encoding="utf-8"))
    source["articles"][0]["published_at"] = "2026-09-29T08:59:00+09:00"
    content.write_text(json.dumps(source, ensure_ascii=False), encoding="utf-8")
    data = json.loads(manifest.read_text(encoding="utf-8"))
    data["input_sha256"] = _sha(content.read_bytes())
    manifest.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(personal.PersonalBriefError, match="out-of-window"):
        personal.prepare_personal_email(manifest, tmp_path / "evening_mail_v1",
                                        workspace_root=tmp_path,
                                        now=datetime(2026, 9, 29, 13, tzinfo=UTC),
                                        edition="evening")


def test_resend_after_confirmed_send_runs_once(tmp_path: Path, smtp: list[list[str]]) -> None:
    manifest = _fixture(tmp_path)
    ledger = tmp_path / "attempts.sqlite3"
    _original_issue(ledger, "SENT")
    first = tmp_path / "resend_v1"
    personal.prepare_personal_email(
        manifest, first, resend_reason="섹션별 1페이지 수정본", workspace_root=tmp_path, now=NOW
    )
    message = BytesParser(policy=policy.default).parsebytes((first / "message.eml").read_bytes())
    assert message["Subject"].endswith("| 수정본") and message["X-KETS-Resend"] == "true"
    result = personal.send_personal_email(
        first, allow_resend=True, workspace_root=tmp_path, log_path=ledger, now=NOW
    )
    assert result["resend"] is True and smtp == [[ACCOUNT]]
    second = tmp_path / "resend_v2"
    personal.prepare_personal_email(
        manifest, second, resend_reason="같은 판본 재시도", workspace_root=tmp_path, now=NOW
    )
    with pytest.raises(DeliveryError, match="already resent"):
        personal.send_personal_email(
            second, allow_resend=True, workspace_root=tmp_path, log_path=ledger, now=NOW
        )
    assert smtp == [[ACCOUNT]]
    with sqlite3.connect(ledger) as connection:
        rows = connection.execute(
            "SELECT state, reason FROM personal_resend_reservations"
        ).fetchall()
        original = connection.execute("SELECT state FROM daily_issue_reservations").fetchall()
    connection.close()
    assert rows == [("SENT", "섹션별 1페이지 수정본")]
    assert original == [("SENT",)]


@pytest.mark.parametrize("state", ["ATTEMPTING", "UNCERTAIN"])
def test_unconfirmed_original_blocks_resend(
    tmp_path: Path, smtp: list[list[str]], state: str,
) -> None:
    ledger = tmp_path / "attempts.sqlite3"
    _original_issue(ledger, state)
    output = tmp_path / "resend_v1"
    personal.prepare_personal_email(
        _fixture(tmp_path), output, resend_reason="수정본", workspace_root=tmp_path, now=NOW
    )
    with pytest.raises(DeliveryError, match="recorded as SENT"):
        personal.send_personal_email(
            output, allow_resend=True, workspace_root=tmp_path, log_path=ledger, now=NOW
        )
    assert smtp == []


def test_resend_flag_must_match_draft_before_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest = _fixture(tmp_path)
    plain = tmp_path / "mail_v1"
    marked = tmp_path / "resend_v1"
    personal.prepare_personal_email(manifest, plain, workspace_root=tmp_path, now=NOW)
    personal.prepare_personal_email(
        manifest, marked, resend_reason="수정본", workspace_root=tmp_path, now=NOW
    )
    monkeypatch.setattr(
        personal, "load_password", lambda: pytest.fail("credentials must not be read")
    )
    with pytest.raises(personal.PersonalBriefError, match="allow-resend"):
        personal.send_personal_email(plain, allow_resend=True, workspace_root=tmp_path, now=NOW)
    with pytest.raises(personal.PersonalBriefError, match="allow-resend"):
        personal.send_personal_email(marked, workspace_root=tmp_path, now=NOW)


def test_non_v7_brief_is_rejected(tmp_path: Path) -> None:
    manifest = _fixture(tmp_path)
    data = json.loads(manifest.read_text(encoding="utf-8"))
    data["layout"] = "main_and_headlines_v6"
    manifest.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(personal.PersonalBriefError, match="section-page"):
        personal.prepare_personal_email(
            manifest, tmp_path / "mail_v1", workspace_root=tmp_path, now=NOW
        )


def test_delivery_log_includes_packaged_app_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    standard = tmp_path / "KETS-Intelligence" / "newsletter-smtp-delivery.sqlite3"
    assert personal.resolve_delivery_log() == standard
    virtual = (tmp_path / "Packages" / "Example.App_1" / "LocalCache" / "Local"
               / "KETS-Intelligence" / "newsletter-smtp-delivery.sqlite3")
    virtual.parent.mkdir(parents=True)
    virtual.write_bytes(b"ledger")
    assert personal.resolve_delivery_log() == virtual
    standard.parent.mkdir(parents=True)
    os.link(virtual, standard)
    assert personal.resolve_delivery_log() == standard
    standard.unlink()
    standard.write_bytes(b"other ledger")
    with pytest.raises(personal.PersonalBriefError, match="More than one delivery log"):
        personal.resolve_delivery_log()
