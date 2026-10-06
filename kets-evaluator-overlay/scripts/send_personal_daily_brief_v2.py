"""Prepare, then explicitly send the v7 section-page brief to its owner's mailbox.

Design Ref: operator request 2026-09-29. The HTML body follows the PDF: one block
per news section with at most two summaries and title links for other articles.
Plan SC: a second mail for an issue already recorded as SENT needs a resend reason
when the draft is prepared and --allow-resend when it is sent. Each brief PDF can
be resent once, and an ATTEMPTING or UNCERTAIN original blocks any resend.
The v1 sender, its drafts and its receipts remain unchanged.
"""

from __future__ import annotations

import argparse
import contextlib
import html
import json
import os
import smtplib
import sqlite3
import ssl
import sys
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from email.utils import format_datetime, make_msgid, parseaddr
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from kets_intelligence.application.smtp_delivery import (  # noqa: E402
    ACCOUNT,
    SMTP_HOST,
    SMTP_PORT,
    DeliveryError,
    PreparedMail,
    _open_log,
    _record,
    _reserve,
    delivery_log_path,
)
from kets_intelligence.sources.windows_credential import (  # noqa: E402
    CredentialError,
    load_password,
)
from scripts import send_personal_daily_brief_v1 as v1  # noqa: E402

PersonalBriefError = v1.PersonalBriefError
LAYOUT = "section_page_two_summaries_v7"
MAX_SUMMARIES = 2
SCHEMA_VERSION = 2
SUBJECT = "이너젠 탄소·에너지 일간 브리프 | {issue}"
RESEND_SUBJECT_SUFFIX = " | 수정본"
RESEND_TABLE = """CREATE TABLE IF NOT EXISTS personal_resend_reservations (
    recipient TEXT NOT NULL,
    issue_date TEXT NOT NULL,
    pdf_sha256 TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    reason TEXT NOT NULL,
    state TEXT NOT NULL,
    reserved_at_utc TEXT NOT NULL,
    completed_at_utc TEXT,
    PRIMARY KEY (recipient, issue_date, pdf_sha256)
)"""


def resolve_delivery_log() -> Path:
    """Find the one existing ledger, including a packaged app's virtualized copy.

    Plan SC: an MSIX desktop app writes %LOCALAPPDATA% under
    Packages/<app>/LocalCache/Local. Reading only the plain path from another
    process would miss sends recorded there, so two different ledgers fail closed.
    """
    standard = delivery_log_path()
    local = standard.parents[1]
    candidates = [standard, *sorted(
        local.glob(f"Packages/*/LocalCache/Local/{standard.parent.name}/{standard.name}")
    )]
    found: list[Path] = []
    for path in candidates:
        if path.is_file() and not any(os.path.samefile(path, known) for known in found):
            found.append(path)
    if len(found) > 1:
        raise PersonalBriefError(
            "More than one delivery log exists; pass --delivery-log explicitly"
        )
    return found[0] if found else standard


def _reason(value: str) -> str:
    reason = " ".join(value.split())
    if not 0 < len(reason) <= 200:
        raise PersonalBriefError("resend reason must be 1-200 characters")
    return reason


def _source_date(article: Mapping[str, Any]) -> str:
    """Match the PDF: a source clock time in KST when supplied, else its date."""
    if article.get("published_at"):
        published = v1._aware(article["published_at"], "article publication time")
        return f"{published.astimezone(v1.KST):%Y-%m-%d %H:%M} KST"
    return str(article["published_date"])


def _check_layout(manifest: Mapping[str, Any], source: Mapping[str, Any],
                  layout: str = LAYOUT) -> None:
    if manifest.get("layout") != layout:
        raise PersonalBriefError("brief is not the v7 section-page edition")
    summary_ids = set(manifest["main_article_ids"])
    counts = Counter(
        article["section"] for article in source["articles"] if article["id"] in summary_ids
    )
    if any(count > MAX_SUMMARIES for count in counts.values()):
        raise PersonalBriefError("a section has more than two summaries")


def _bullet_html(bullet: str, phrase: object) -> str:
    escaped = html.escape(bullet)
    if isinstance(phrase, str) and phrase and bullet.count(phrase) == 1:
        marked = html.escape(phrase)
        return escaped.replace(marked, f"<strong>{marked}</strong>", 1)
    return escaped


def _kau_html(source: Mapping[str, Any]) -> list[str]:
    """Render the market block exactly as the v1 personal edition did."""
    e = html.escape
    kau = source["kau"]
    lines = [
        '<h2>KAU 일별 가격·거래 및 수급</h2>',
        f'<p>{e(str(kau["instrument"]))} · {e(str(kau["venue"]))} · '
        f'{e(str(kau["price_unit"]))} · {e(str(kau["volume_unit"]))}</p>',
        '<table><thead><tr><th>거래일</th><th>종가</th><th>거래량</th></tr></thead><tbody>',
    ]
    for row in kau["observations"]:
        lines.append(
            f'<tr><td>{e(str(row["trade_date"]))}</td>'
            f'<td>{e(str(row["close_krw_per_tco2e"]))}</td>'
            f'<td>{e(str(row["volume_tco2e"]))}</td></tr>'
        )
    lines.append("</tbody></table>")
    for key in ("reader_source_note", "reader_pressure_note", "reader_methodology_note"):
        if kau.get(key):
            lines.append(f'<p>{e(str(kau[key]))}</p>')
    for key, label in (("buy_pressure", "매수세"), ("sell_pressure", "매도세")):
        lines.append(f'<p><strong>{label}</strong> {e(str(kau[key]["summary"]))}</p>')
    for interpretation in kau.get("interpretations", []):
        if isinstance(interpretation, dict):
            for key, label in (("text", "분석"), ("alternative", "대안"),
                               ("invalidation", "판단 변경")):
                if interpretation.get(key):
                    lines.append(f'<p><strong>{label}</strong> {e(str(interpretation[key]))}</p>')
    return lines


def _html_body(source: Mapping[str, Any], summary_ids: Sequence[str]) -> str:
    """Render each section as summaries first, then title links, from pinned JSON."""
    e = html.escape
    articles = source["articles"]
    summaries = set(summary_ids)
    lines = [
        '<!doctype html><html lang="ko"><head><meta charset="utf-8">',
        '<style>body{font-family:Arial,sans-serif;max-width:760px;margin:auto;color:#1f2933;'
        'line-height:1.55}h1{font-size:24px}h2{border-bottom:2px solid #173752;padding-top:24px}'
        'h3{font-size:17px;margin-bottom:4px}table{border-collapse:collapse;width:100%}'
        'th,td{border:1px solid #d6dce1;padding:7px;text-align:right}th:first-child,'
        'td:first-child{text-align:left}.meta{color:#536471}li{margin-bottom:7px}'
        '.more{font-weight:bold;margin:18px 0 4px}</style>',
        '</head><body><h1>이너젠 탄소·에너지 일간 브리프</h1>',
        f'<p class="meta">{e(str(source["issue_date"]))} · '
        f'{e(str(source["cutoff_at"]))} 기준</p>',
        *_kau_html(source),
    ]
    for section in v1.SECTIONS:
        items = [article for article in articles if article["section"] == section]
        if not items:
            continue
        lines.append(f"<h2>{e(section)}</h2>")
        picked = [article for article in items if article["id"] in summaries]
        others = [article for article in items if article["id"] not in summaries]
        for article in picked:
            phrases = article.get("emphasis") or [""] * len(article["bullets"])
            lines.extend((
                f'<h3>{e(article["headline"])}</h3>',
                f'<p class="meta">{e(str(article["publisher"]))} · '
                f'{e(_source_date(article))}</p>',
                "<ul>",
            ))
            lines.extend(
                f"<li>{_bullet_html(bullet, phrase)}</li>"
                for bullet, phrase in zip(article["bullets"], phrases, strict=True)
            )
            lines.extend((
                "</ul>",
                f'<p><a href="{e(article["url"], quote=True)}">원문 기사 확인</a></p>',
            ))
        if others:
            lines.extend((
                f'<p class="more">{"그 밖의 기사" if picked else "기사"}</p>', "<ul>",
            ))
            lines.extend(
                f'<li><a href="{e(article["url"], quote=True)}">{e(article["headline"])}</a>'
                f' <span class="meta">· {e(str(article["publisher"]))} · '
                f'{e(_source_date(article))}</span></li>'
                for article in others
            )
            lines.append("</ul>")
    lines.append("</body></html>")
    return "\n".join(lines) + "\n"


@dataclass(frozen=True)
class MailProfile:
    """How one sender version renders its HTML; the send gates stay identical.

    Design Ref: v3 adds a designed body and an inline logo without changing the
    v2 drafts, which keep schema 2 and the plain renderer below.
    """

    schema_version: int
    render: Callable[[Mapping[str, Any], Sequence[str]], str]
    images: tuple[tuple[str, Path], ...] = ()
    # Reader-facing attachment name for an issue date; None keeps the build file name.
    file_stem: Callable[[str], str] | None = None
    # Design Ref: the v9 pilot brief adds an overseas page; its sender pins that layout.
    layout: str = LAYOUT


V2_PROFILE = MailProfile(SCHEMA_VERSION, _html_body)


def _image_pins(profile: MailProfile) -> list[dict[str, str]]:
    return [{"cid": cid, "path": str(path), "sha256": v1._digest(path.read_bytes())}
            for cid, path in profile.images]


PPTX_TYPE = ("application", "vnd.openxmlformats-officedocument.presentationml.presentation")


def _companion(brief: Mapping[str, Any], root: Path) -> tuple[Path, bytes] | None:
    """An optional PPTX pinned in the brief manifest travels with the PDF."""
    if not brief.get("pptx"):
        return None
    return v1._pinned_input(brief, "pptx", "pptx_sha256", root)


def _subject(issue: str, resend: bool, edition: str = "morning") -> str:
    if edition == "evening":
        return SUBJECT.format(issue=issue) + " | 21:00 저녁판"
    return SUBJECT.format(issue=issue) + (RESEND_SUBJECT_SUFFIX if resend else "")


def _delivery_slot(issue: str, edition: str) -> str:
    """Use a separate one-send ledger key for the reviewed 21:00 edition."""
    return f"{issue} 21:00" if edition == "evening" else issue


def _check_edition_window(source: Mapping[str, Any], edition: str) -> None:
    if edition != "evening":
        return
    cutoff = v1._aware(source["cutoff_at"], "brief cutoff").astimezone(v1.KST)
    start = cutoff.replace(hour=9, minute=0, second=0, microsecond=0)
    if (cutoff.hour, cutoff.minute, cutoff.second) != (21, 0, 0):
        raise PersonalBriefError("evening edition requires a 21:00 KST brief cutoff")
    if v1._aware(source.get("window_start"), "brief window start") != start:
        raise PersonalBriefError("evening edition requires a 09:00 KST publication start")
    # Plan SC: date-only stories cannot prove that they were published after 09:00.
    for article in source["articles"]:
        published = v1._aware(article.get("published_at"), "article publication time")
        if not start <= published < cutoff:
            raise PersonalBriefError("evening brief contains an out-of-window article")


def prepare_personal_email(
    brief_manifest_path: Path, output_dir: Path, *, resend_reason: str | None = None,
    workspace_root: Path = ROOT, now: datetime | None = None,
    profile: MailProfile = V2_PROFILE, edition: str = "morning",
) -> dict[str, Any]:
    """Save an immutable local mail draft; this function never uses the network."""
    current = now or datetime.now(UTC)
    if current.tzinfo is None or current.utcoffset() is None:
        raise PersonalBriefError("current time needs a timezone")
    brief, source, pdf_path, pdf_raw, markdown_raw, brief_manifest_raw = v1._brief_inputs(
        brief_manifest_path, workspace_root, current
    )
    _check_layout(brief, source, profile.layout)
    if edition not in {"morning", "evening"}:
        raise PersonalBriefError("edition must be morning or evening")
    _check_edition_window(source, edition)
    reason = None if resend_reason is None else _reason(resend_reason)
    if edition == "evening" and reason is not None:
        raise PersonalBriefError("evening edition has its own slot; do not mark it as a resend")
    output = output_dir.resolve(strict=False)
    if not output.is_relative_to(workspace_root.resolve(strict=True)) or output.exists():
        raise PersonalBriefError("output must be a new versioned workspace directory")
    html_body = profile.render(source, brief["main_article_ids"])
    message = EmailMessage()
    message["From"] = ACCOUNT
    message["To"] = ACCOUNT
    message["Subject"] = _subject(brief["issue_date"], reason is not None, edition)
    message["Date"] = format_datetime(current.astimezone(v1.KST))
    message["Message-ID"] = make_msgid(domain="inng.co.kr")
    message["X-KETS-Personal-Review"] = "true"
    if edition == "evening":
        message["X-KETS-Edition"] = "evening"
    if reason is not None:
        message["X-KETS-Resend"] = "true"
    message.set_content(v1._lines(markdown_raw.decode("utf-8")), charset="utf-8")
    message.add_alternative(html_body, subtype="html", charset="utf-8")
    if profile.images:
        payload = message.get_payload()
        html_part = payload[1] if isinstance(payload, list) else None
        if not isinstance(html_part, EmailMessage):
            raise PersonalBriefError("HTML part is missing for inline images")
        for cid, path in profile.images:
            html_part.add_related(path.read_bytes(), maintype="image",
                                  subtype=path.suffix.lstrip(".").lower(), cid=f"<{cid}>")
    stem = profile.file_stem(brief["issue_date"]) if profile.file_stem else None
    if stem and edition == "evening":
        stem += "_2100"
    message.add_attachment(
        pdf_raw, maintype="application", subtype="pdf",
        filename=f"{stem}.pdf" if stem else pdf_path.name,
    )
    companion = _companion(brief, workspace_root)
    if companion is not None:
        message.add_attachment(companion[1], maintype=PPTX_TYPE[0], subtype=PPTX_TYPE[1],
                               filename=f"{stem}.pptx" if stem else companion[0].name)
    eml_raw = message.as_bytes(policy=policy.SMTP)
    if not 0 < len(eml_raw) <= v1.MAX_MAIL_BYTES:
        raise PersonalBriefError("prepared email is too large")
    prepared: dict[str, Any] = {
        "schema_version": profile.schema_version,
        "status": "READY_FOR_PERSONAL_SEND",
        "layout": profile.layout,
        "resend": reason is not None,
        "resend_reason": reason,
        "edition": edition,
        "delivery_slot": _delivery_slot(brief["issue_date"], edition),
        "issue_date": brief["issue_date"],
        "prepared_at_utc": current.astimezone(UTC).isoformat(),
        "from_email": ACCOUNT,
        "to_email": ACCOUNT,
        "customer_distribution_approved": False,
        "brief_manifest_path": str(brief_manifest_path.resolve(strict=True)),
        "brief_manifest_sha256": v1._digest(brief_manifest_raw),
        "input_path": brief["input_path"],
        "input_sha256": brief["input_sha256"],
        "pdf_path": brief["pdf"],
        "pdf_sha256": brief["pdf_sha256"],
        "pdf_pages": brief["pdf_pages"],
        "markdown_path": brief["markdown"],
        "markdown_sha256": brief["markdown_sha256"],
        "article_count": len(source["articles"]),
        "summary_count": len(brief["main_article_ids"]),
        "link_count": len(brief["additional_headline_ids"]),
        "pptx_sha256": brief.get("pptx_sha256"),
        "html_sha256": v1._digest(html_body.encode("utf-8")),
        "eml_sha256": v1._digest(eml_raw),
        "message_id": str(message["Message-ID"]),
    }
    if profile.images:
        prepared["inline_images"] = _image_pins(profile)
    output.mkdir(parents=True, exist_ok=False)
    for name, raw in (
        ("message.eml", eml_raw),
        ("body.html", html_body.encode("utf-8")),
        ("personal_mail_manifest.json", v1._json_bytes(prepared)),
    ):
        with (output / name).open("xb") as handle:
            handle.write(raw)
    return {"status": prepared["status"], "output": str(output), "resend": prepared["resend"],
            "article_count": prepared["article_count"], "emails_sent": 0}


def _prepared_mail(
    prepared_dir: Path, root: Path, now: datetime, profile: MailProfile = V2_PROFILE,
) -> tuple[PreparedMail, Path, dict[str, Any]]:
    """Recheck every pinned byte before credentials are read."""
    output = prepared_dir.resolve(strict=True)
    if not output.is_relative_to(root.resolve(strict=True)) or not output.is_dir():
        raise PersonalBriefError("prepared directory must be in this workspace")
    prepared_path = v1._in_workspace(output / "personal_mail_manifest.json", root, "mail manifest")
    try:
        prepared = json.loads(prepared_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        raise PersonalBriefError("mail manifest is unreadable") from exc
    if not isinstance(prepared, dict) or prepared.get("schema_version") != profile.schema_version:
        raise PersonalBriefError(
            f"mail manifest is not a v{profile.schema_version} personal draft"
        )
    if prepared.get("inline_images", []) != _image_pins(profile):
        raise PersonalBriefError("inline images changed after preparation")
    resend = prepared.get("resend")
    edition = prepared.get("edition", "morning")
    if (
        prepared.get("status") != "READY_FOR_PERSONAL_SEND"
        or prepared.get("layout") != profile.layout
        or prepared.get("from_email") != ACCOUNT
        or prepared.get("to_email") != ACCOUNT
        or prepared.get("customer_distribution_approved") is not False
        or not isinstance(resend, bool)
        or edition not in {"morning", "evening"}
        or (edition == "evening" and resend)
        or (resend and _reason(str(prepared.get("resend_reason"))) != prepared["resend_reason"])
        or (not resend and prepared.get("resend_reason") is not None)
    ):
        raise PersonalBriefError("mail is not approved for personal self-delivery")
    age = now.astimezone(UTC) - v1._aware(prepared.get("prepared_at_utc"), "prepared time")
    if age < -timedelta(minutes=5) or age > timedelta(hours=48):
        raise PersonalBriefError("mail draft is outside its delivery window")
    brief_path = v1._in_workspace(
        Path(str(prepared.get("brief_manifest_path"))), root, "brief manifest"
    )
    if v1._digest(brief_path.read_bytes()) != prepared.get("brief_manifest_sha256"):
        raise PersonalBriefError("brief manifest changed after email preparation")
    brief, source, _, pdf_raw, markdown_raw, _ = v1._brief_inputs(brief_path, root, now)
    _check_layout(brief, source, profile.layout)
    _check_edition_window(source, edition)
    if (
        prepared.get("issue_date") != brief["issue_date"]
        or prepared.get("delivery_slot", _delivery_slot(brief["issue_date"], edition))
        != _delivery_slot(brief["issue_date"], edition)
        or prepared.get("pdf_sha256") != brief["pdf_sha256"]
        or prepared.get("markdown_sha256") != brief["markdown_sha256"]
        or prepared.get("input_sha256") != brief["input_sha256"]
    ):
        raise PersonalBriefError("mail and brief source pins disagree")
    expected_html = profile.render(source, brief["main_article_ids"]).encode("utf-8")
    body_path = v1._in_workspace(output / "body.html", root, "email HTML")
    if (
        body_path.read_bytes() != expected_html
        or prepared.get("html_sha256") != v1._digest(expected_html)
    ):
        raise PersonalBriefError("email HTML changed after preparation")
    eml_path = v1._in_workspace(output / "message.eml", root, "prepared email")
    eml_raw = eml_path.read_bytes()
    if (
        not 0 < len(eml_raw) <= v1.MAX_MAIL_BYTES
        or v1._digest(eml_raw) != prepared.get("eml_sha256")
    ):
        raise PersonalBriefError("prepared email changed after review")
    parsed = BytesParser(policy=policy.default).parsebytes(eml_raw)
    if not isinstance(parsed, EmailMessage) or parsed.defects:
        raise PersonalBriefError("prepared email format is invalid")
    if any(parsed.get_all(key) for key in ("Cc", "Bcc", "Reply-To", "Sender", "X-Unsent")):
        raise PersonalBriefError("prepared email has an extra address or draft marker")
    if any(key.lower().startswith("resent-") for key in parsed):
        raise PersonalBriefError("prepared email has a resent address header")
    for key in ("From", "To"):
        if parseaddr(v1._one_header(parsed, key)) != ("", ACCOUNT):
            raise PersonalBriefError("sender and recipient must be the same company mailbox")
    if v1._one_header(parsed, "X-KETS-Personal-Review") != "true":
        raise PersonalBriefError("personal-review mail marker is missing")
    if parsed.get("X-KETS-Edition") != ("evening" if edition == "evening" else None):
        raise PersonalBriefError("mail edition marker differs from the draft manifest")
    if (parsed.get("X-KETS-Resend") == "true") != resend or (
        v1._one_header(parsed, "Subject") != _subject(brief["issue_date"], resend, edition)
    ):
        raise PersonalBriefError("resend marker or subject differs from the draft manifest")
    message_id = v1._one_header(parsed, "Message-ID")
    if message_id != prepared.get("message_id"):
        raise PersonalBriefError("message ID changed after preparation")
    plain = [part for part in parsed.walk() if part.get_content_type() == "text/plain"]
    rich = [part for part in parsed.walk() if part.get_content_type() == "text/html"]
    pdf = [
        part for part in parsed.iter_attachments()
        if part.get_content_type() == "application/pdf"
    ]
    decks = [part.get_payload(decode=True) for part in parsed.iter_attachments()
             if part.get_content_type() == "/".join(PPTX_TYPE)]
    companion = _companion(brief, root)
    if (prepared.get("pptx_sha256") != brief.get("pptx_sha256")
            or decks != ([companion[1]] if companion else [])):
        raise PersonalBriefError("PPTX attachment differs from the pinned brief")
    images = {str(part.get("Content-ID", "")): part.get_payload(decode=True)
              for part in parsed.walk() if part.get_content_maintype() == "image"}
    if images != {f"<{cid}>": path.read_bytes() for cid, path in profile.images}:
        raise PersonalBriefError("inline images differ from the pinned files")
    if (
        len(plain) != 1 or len(rich) != 1 or len(pdf) != 1
        or v1._lines(plain[0].get_content()) != v1._lines(markdown_raw.decode("utf-8"))
        or v1._lines(rich[0].get_content()) != v1._lines(expected_html.decode("utf-8"))
        or pdf[0].get_payload(decode=True) != pdf_raw
    ):
        raise PersonalBriefError("email body or PDF differs from the pinned reader files")
    return PreparedMail(
        raw=eml_raw, sha256=v1._digest(eml_raw), message_id=message_id,
        subject=v1._one_header(parsed, "Subject"),
        issue_date=_delivery_slot(brief["issue_date"], edition),
    ), output, prepared


def _reserve_resend(
    connection: sqlite3.Connection, mail: PreparedMail, pdf_sha256: str, reason: str,
) -> None:
    """Record the resend before SMTP DATA, only after a confirmed original send."""
    connection.execute(RESEND_TABLE)
    connection.commit()
    try:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT state FROM daily_issue_reservations WHERE recipient = ? AND issue_date = ?",
            (ACCOUNT, mail.issue_date),
        ).fetchone()
        if row is None or row[0] != "SENT":
            connection.rollback()
            raise DeliveryError(
                "A resend needs an original delivery recorded as SENT; inspect the mailbox"
            )
        attempted = datetime.now(UTC).isoformat()
        connection.execute(
            """INSERT INTO delivery_attempts
            (sha256, message_id, sender, recipient, state, attempted_at_utc)
            VALUES (?, ?, ?, ?, 'ATTEMPTING', ?)""",
            (mail.sha256, mail.message_id, ACCOUNT, ACCOUNT, attempted),
        )
        connection.execute(
            """INSERT INTO personal_resend_reservations
            (recipient, issue_date, pdf_sha256, sha256, reason, state, reserved_at_utc)
            VALUES (?, ?, ?, ?, ?, 'ATTEMPTING', ?)""",
            (ACCOUNT, mail.issue_date, pdf_sha256, mail.sha256, reason, attempted),
        )
        connection.commit()
    except sqlite3.IntegrityError as exc:
        connection.rollback()
        raise DeliveryError(
            "This brief edition or message was already resent or attempted; "
            "review the delivery log"
        ) from exc


def _record_resend(
    connection: sqlite3.Connection, mail: PreparedMail, pdf_sha256: str, state: str,
) -> None:
    completed = datetime.now(UTC).isoformat()
    connection.execute(
        "UPDATE delivery_attempts SET state = ?, completed_at_utc = ? WHERE sha256 = ?",
        (state, completed, mail.sha256),
    )
    connection.execute(
        """UPDATE personal_resend_reservations SET state = ?, completed_at_utc = ?
        WHERE recipient = ? AND issue_date = ? AND pdf_sha256 = ?""",
        (state, completed, ACCOUNT, mail.issue_date, pdf_sha256),
    )
    connection.commit()


def send_personal_email(
    prepared_dir: Path, *, allow_resend: bool = False, workspace_root: Path = ROOT,
    log_path: Path | None = None, now: datetime | None = None,
    profile: MailProfile = V2_PROFILE,
) -> dict[str, Any]:
    """Send one reviewed self-mail; uncertain SMTP outcomes cannot be retried."""
    current = now or datetime.now(UTC)
    if current.tzinfo is None or current.utcoffset() is None:
        raise PersonalBriefError("current time needs a timezone")
    mail, output, prepared = _prepared_mail(prepared_dir, workspace_root, current, profile)
    resend = prepared["resend"] is True
    if resend != allow_resend:
        raise PersonalBriefError(
            "a resend draft needs --allow-resend, and a first send must not use it"
        )
    ledger = log_path or resolve_delivery_log()
    pdf_sha256 = str(prepared["pdf_sha256"])
    password = load_password()
    try:
        smtp = smtplib.SMTP_SSL(
            SMTP_HOST, SMTP_PORT, timeout=20, context=ssl.create_default_context()
        )
        try:
            smtp.login(ACCOUNT, password)
            with contextlib.closing(_open_log(ledger)) as connection:
                # Plan SC: reserve before DATA; any crash leaves a blocking claim.
                if resend:
                    _reserve_resend(connection, mail, pdf_sha256, str(prepared["resend_reason"]))
                else:
                    _reserve(connection, mail)
                try:
                    if smtp.sendmail(ACCOUNT, [ACCOUNT], mail.raw):
                        raise PersonalBriefError("SMTP refused the personal recipient")
                except (OSError, smtplib.SMTPException, PersonalBriefError) as exc:
                    if resend:
                        _record_resend(connection, mail, pdf_sha256, "UNCERTAIN")
                    else:
                        _record(connection, mail, "UNCERTAIN")
                    raise PersonalBriefError(
                        "SMTP outcome is uncertain; inspect the mailbox before retrying"
                    ) from exc
                if resend:
                    _record_resend(connection, mail, pdf_sha256, "SENT")
                else:
                    _record(connection, mail, "SENT")
        finally:
            smtp.close()
    except (OSError, smtplib.SMTPException) as exc:
        raise PersonalBriefError("SMTP connection or authentication failed") from exc
    receipt = {
        "state": "SENT", "message_id": mail.message_id, "sha256": mail.sha256,
        "issue_date": prepared["issue_date"], "edition": prepared.get("edition", "morning"),
        "delivery_slot": mail.issue_date, "from_email": ACCOUNT, "to_email": ACCOUNT,
        "resend": resend, "delivery_log": str(ledger),
        "accepted_at_utc": datetime.now(UTC).isoformat(),
        "note": "SMTP acceptance; inbox arrival is not confirmed",
    }
    receipt_status = "saved"
    try:
        with (output / "delivery_receipt.json").open("xb") as handle:
            handle.write(v1._json_bytes(receipt))
    except OSError:
        receipt_status = "log_only"
    return {"state": "SENT", "message_id": mail.message_id, "sha256": mail.sha256,
            "resend": resend, "receipt_status": receipt_status}


def main(argv: Sequence[str] | None = None, profile: MailProfile = V2_PROFILE) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--brief-manifest", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--resend-reason", help="Prepare a marked resend of a SENT issue")
    parser.add_argument("--edition", choices=("morning", "evening"), default="morning",
                        help="Prepare a separate 21:00 edition when evening")
    parser.add_argument("--prepared", type=Path)
    parser.add_argument("--send", action="store_true", help="Explicitly send a reviewed draft")
    parser.add_argument("--allow-resend", action="store_true",
                        help="Confirm sending a draft prepared with --resend-reason")
    parser.add_argument("--delivery-log", type=Path)
    args = parser.parse_args(argv)
    if args.send:
        if (args.prepared is None or args.brief_manifest or args.output or args.resend_reason
                or args.edition != "morning"):
            parser.error("--send takes --prepared, optionally --allow-resend/--delivery-log")
    elif (args.brief_manifest is None or args.output is None or args.prepared is not None
          or args.allow_resend or args.delivery_log is not None):
        parser.error("preparation requires --brief-manifest and --output")
    try:
        if args.send:
            assert isinstance(args.prepared, Path)
            result: dict[str, Any] = send_personal_email(
                args.prepared, allow_resend=args.allow_resend, log_path=args.delivery_log,
                profile=profile,
            )
        else:
            assert isinstance(args.brief_manifest, Path)
            assert isinstance(args.output, Path)
            result = prepare_personal_email(
                args.brief_manifest, args.output, resend_reason=args.resend_reason,
                profile=profile, edition=args.edition,
            )
    except (PersonalBriefError, DeliveryError, CredentialError, OSError, ValueError) as exc:
        print(json.dumps({"state": "BLOCKED", "message": str(exc)}, ensure_ascii=False))
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
