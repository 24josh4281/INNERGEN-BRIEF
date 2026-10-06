"""Daily-brief candidates, editor assembly and the designed email stay fail-closed."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
from scripts import assemble_daily_brief_content_v1 as assemble
from scripts import prepare_daily_brief_candidates_v1 as candidates
from scripts import send_personal_daily_brief_v3 as designed

CUTOFF = datetime.fromisoformat("2026-09-30T09:00:00+09:00")


def _row(article_id: str, **overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "article_id": article_id, "title": f"{article_id} 배출권 기사", "publisher": "매체",
        "url": f"https://example.org/{article_id}", "published_at_kst": "2026-09-29T10:00:00+09:00",
        "published_at_raw": "2026-09-29T10:00:00+09:00", "date_precision": "second",
        "date_status": "in_window", "priority": "A", "topic_tier": "carbon_core",
        "source_id": "SRC", "observed_at": "2026-09-30T00:01:00+00:00",
    }
    row.update(overrides)
    return row


def test_candidate_filter_drops_repeats_http_and_boundary_dates() -> None:
    rows = [
        _row("fresh"),
        _row("seen"),
        _row("plain", url="http://example.org/plain"),
        _row("old", published_at_kst="2026-09-28T08:00:00+09:00"),
        _row("dateonly", date_precision="day", published_at_kst="", published_at_raw="2026-09-29"),
        _row("boundary", date_precision="day", published_at_kst="", published_at_raw="2026-09-28"),
    ]
    kept, skipped = candidates.eligible_rows(rows, CUTOFF, {"seen"})
    assert sorted(row["article_id"] for row in kept) == ["dateonly", "fresh"]
    assert skipped == {"earlier_issue": 1, "not_https": 1, "outside_48_hours": 1,
                       "date_only_on_boundary": 1}


def test_evening_candidates_exclude_morning_and_unproven_same_day_time() -> None:
    start = datetime.fromisoformat("2026-10-06T09:00:00+09:00")
    cutoff = datetime.fromisoformat("2026-10-06T21:00:00+09:00")
    rows = [
        _row("at-start", published_at_kst=start.isoformat()),
        _row("before", published_at_kst="2026-10-06T08:59:59+09:00"),
        _row("at-end", published_at_kst=cutoff.isoformat()),
        _row("date-only", date_precision="day", published_at_kst="",
             published_at_raw="2026-10-06"),
    ]
    kept, skipped = candidates.eligible_rows(rows, cutoff, set(), start)
    assert [item["article_id"] for item in kept] == ["at-start"]
    assert skipped == {"outside_publication_window": 2, "date_only_on_boundary": 1}


def test_disclosure_page_excludes_exchange_filings() -> None:
    def review_queue(_row: object) -> dict[str, Any]:
        return {"section": "분류 확인", "basis": "test"}

    assert candidates.section_for({"title": "ISSB 기반 지속가능성 공시 로드맵"},
                                  review_queue)[0] == "기후 공시"
    assert candidates.section_for({"title": "[개장 전 주요 공시] 제일기획"},
                                  review_queue)[0] == "기타"


def test_default_selection_leads_with_summary_eligible_items() -> None:
    pool = [{"id": "blocked", "rights_hint": "AI_USE_PROHIBITED"},
            {"id": "a", "rights_hint": "CHECK_ON_READ"},
            {"id": "b", "rights_hint": "CHECK_ON_READ"}]
    selected, summaries = candidates.select_section(pool, 3)
    assert summaries == ["a", "b"]
    assert selected == ["a", "b", "blocked"]


def _write(path: Path, value: object) -> Path:
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    return path


def _files(tmp_path: Path, review: dict[str, Any]) -> tuple[Path, Path, Path]:
    collection = _write(tmp_path / "articles.json", [
        _row("s1"), _row("l1"), _row("plain", url="http://example.org/plain"),
    ])
    pool = [
        {"id": article_id, "title": f"{article_id} 제목", "publisher": "매체",
         "url": f"https://example.org/{article_id}", "published_at": "2026-09-29T10:00:00+09:00",
         "published_date": "2026-09-29", "source_id": "SRC"}
        for article_id in ("s1", "l1")
    ]
    cand = _write(tmp_path / "candidates.json", {
        "issue_date": "2026-09-30", "cutoff_at": CUTOFF.isoformat(),
        "collection": {"run_id": "run", "articles_path": str(collection),
                       "articles_sha256": hashlib.sha256(collection.read_bytes()).hexdigest()},
        "sections": {"기타": {"candidates": pool}},
    })
    kau = _write(tmp_path / "kau.json", {"kau": {"observations": [{"trade_date": "2026-09-29"}]}})
    return cand, _write(tmp_path / "review.json", review), kau


def _review(**overrides: Any) -> dict[str, Any]:
    review: dict[str, Any] = {
        "issue_date": "2026-09-30", "reviewed_at_utc": "2026-09-30T02:00:00+00:00",
        "sections": {"기타": ["s1", "l1"]},
        "summaries": {"s1": {"headline": "새 제목", "bullets": ["배출권 가격이 올랐음"] * 4,
                             "emphasis": ["배출권"] * 4}},
        "retrievals": [{"id": "s1", "url": "https://example.org/s1", "status": 200,
                        "robots_allowed": True, "retrieved_at_utc": "2026-09-30T01:00:00+00:00",
                        "sha256": "a" * 64, "ai_use_prohibited": False, "paywall_notice": None}],
    }
    review.update(overrides)
    return review


def test_assembly_keeps_links_and_pins_the_page_read(tmp_path: Path) -> None:
    result = assemble.assemble(*_files(tmp_path, _review()))
    summary, link = result["articles"]
    assert summary["headline"] == "새 제목" and summary["source_headline"] == "s1 제목"
    assert "a" * 64 in summary["source_evidence"]
    assert link["bullets"] == [] and link["review"]["status"] == "METADATA_ONLY"


def test_ai_use_notice_blocks_a_summary(tmp_path: Path) -> None:
    review = _review()
    review["retrievals"][0]["ai_use_prohibited"] = True
    with pytest.raises(assemble.AssemblyError, match="AI-use"):
        assemble.assemble(*_files(tmp_path, review))


def test_http_link_needs_a_verified_https_read(tmp_path: Path) -> None:
    with pytest.raises(assemble.AssemblyError, match="HTTPS"):
        assemble.assemble(*_files(tmp_path, _review(sections={"기타": ["s1", "plain"]})))


def _brief_source() -> dict[str, Any]:
    return {
        "issue_date": "2026-09-30", "cutoff_at": "2026-09-30T09:00:00+09:00",
        "articles": [
            {"id": "s", "section": "국내 정책", "headline": "요약 <제목>", "publisher": "기관",
             "published_date": "2026-09-29", "url": "https://example.org/s?a=1&b=2",
             "bullets": ["가격이 150원 올랐음"] * 4, "emphasis": ["150원"] * 4},
            {"id": "l", "section": "국내 정책", "headline": "링크 기사", "publisher": "매체",
             "published_date": "2026-09-29", "published_at": "2026-09-29T10:00:00+09:00",
             "url": "https://example.org/l", "bullets": [], "emphasis": []},
        ],
        "kau": {
            "instrument": "KAU26", "venue": "KRX", "price_unit": "KRW/tCO2e",
            "volume_unit": "tCO2e",
            "observations": [
                {"trade_date": "2026-09-28", "close_krw_per_tco2e": 29900, "volume_tco2e": 333600},
                {"trade_date": "2026-09-29", "close_krw_per_tco2e": 30050, "volume_tco2e": 109502},
            ],
            "buy_pressure": {"summary": "순매수 10t"}, "sell_pressure": {"summary": "순매도 10t"},
        },
    }


def test_designed_email_escapes_text_and_loads_no_remote_image() -> None:
    body = designed.designed_html(_brief_source(), ["s"])
    assert "cid:innergen-logo@inng.co.kr" in body
    assert 'src="http' not in body
    assert "요약 &lt;제목&gt;" in body and "https://example.org/s?a=1&amp;b=2" in body
    assert "+150원 (+0.50%)" in body and "30,050" in body and "-67.18%" in body
    assert body.index("요약 &lt;제목&gt;") < body.index("그 밖의 기사") < body.index("링크 기사")
    assert "2026-09-29 10:00 KST" in body


def test_designed_profile_pins_the_logo() -> None:
    assert designed.V3_PROFILE.schema_version == 3
    assert designed.V3_PROFILE.images == ((designed.LOGO_CID, designed.LOGO),)
    assert designed.LOGO.is_file()
