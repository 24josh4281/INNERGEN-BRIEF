"""Assemble a v7 daily-brief content file from candidates, an editor review and KAU data.

Design Ref: operator request 2026-09-30 for a 09:00 daily brief. Offline only:
the editor's review JSON supplies the final section lists, the summaries and the
page-read evidence (hashes and rights notices). A summary needs a successful read
without an AI-use prohibition or paywall notice; every other item stays a link.
The KAU block is copied unchanged from an earlier verified content file.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
SECTIONS = (
    "국내 정책", "해외 정책", "국내 산업", "해외 산업",
    "자발적·국제 탄소시장", "재생에너지", "기후 공시", "기타",
)
LAYOUT = "section_page_two_summaries_v7"


class AssemblyError(ValueError):
    """The review cannot become a safe brief input."""


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _ref(path: Path) -> dict[str, str]:
    resolved = path.resolve()
    shown = resolved.relative_to(ROOT) if resolved.is_relative_to(ROOT) else resolved
    return {"path": str(shown), "sha256": _sha(resolved)}


def _load(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise AssemblyError(f"{path} must hold a JSON object")
    return value


def latest_kau_content(root: Path, issue: date) -> Path:
    """Use the newest earlier v7 brief input; its KAU rows are already verified."""
    best: tuple[str, bool, str, Path] | None = None
    for path in (root / "output/newsletter").glob("**/*.manifest.json"):
        try:
            manifest = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValueError):
            continue
        if not isinstance(manifest, dict) or manifest.get("layout") != LAYOUT:
            continue
        issued = str(manifest.get("issue_date", ""))
        content = Path(str(manifest.get("input_path", "")))
        # Prefer the latest issue, then a v8 public-source KAU block, then the newest file.
        key = (issued, manifest.get("builder") == "v8", str(manifest.get("built_at_utc", "")))
        if issued <= issue.isoformat() and content.is_file() and (
            best is None or key > best[:3]
        ):
            best = (*key, content)
    if best is None:
        raise AssemblyError("no earlier v7 brief input with a KAU block was found")
    return best[3]


def _reads(review: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Keep the last successful page read per article."""
    reads: dict[str, dict[str, Any]] = {}
    for row in review.get("retrievals", []):
        if isinstance(row, dict) and row.get("status") == 200:
            reads[str(row["id"])] = row
    return reads


def _metadata(
    article_id: str, candidates: dict[str, dict[str, Any]],
    collection: dict[str, dict[str, Any]], reads: dict[str, dict[str, Any]],
    cutoff: datetime, window_start: datetime,
) -> dict[str, Any]:
    if article_id in candidates:
        return dict(candidates[article_id])
    row = collection.get(article_id)
    if row is None:
        raise AssemblyError(f"{article_id}: not in the pinned collection")
    original = str(row.get("canonical_url") or row.get("url"))
    read = reads.get(article_id)
    if urlsplit(original).scheme == "https":
        url = original
    elif read is not None and urlsplit(str(read["url"])).scheme == "https" and (
        original.split("://", 1)[-1] == str(read["url"]).split("://", 1)[-1]
    ):
        # An http collection link is used only after the same path answered over HTTPS.
        url = str(read["url"])
    else:
        raise AssemblyError(f"{article_id}: HTTPS link was not verified for the same page")
    stamp = str(row.get("published_at_kst") or "")
    if not stamp:
        raise AssemblyError(f"{article_id}: publication clock time is missing")
    published = datetime.fromisoformat(stamp)
    if not window_start <= published < cutoff:
        raise AssemblyError(f"{article_id}: outside the publication window")
    return {
        "id": article_id, "title": row.get("title", ""), "publisher": row.get("publisher", ""),
        "url": url, "published_at": published.isoformat(),
        "published_date": published.date().isoformat(), "source_id": row.get("source_id", ""),
        "observed_at": row.get("observed_at"), "date_evidence_url": row.get("date_evidence_url"),
        "https_verified_by_read": url != original,
    }


def with_period(text: str) -> str:
    stripped = text.rstrip()
    return stripped if stripped.endswith((".", "?", "!")) else stripped + "."


def _summary_fields(
    article_id: str, summary: dict[str, Any], read: dict[str, Any] | None, reviewed_at: str,
) -> dict[str, Any]:
    if read is None or read.get("robots_allowed") is not True:
        raise AssemblyError(f"{article_id}: summary needs a successful robots-allowed page read")
    if read.get("ai_use_prohibited") or read.get("paywall_notice"):
        raise AssemblyError(f"{article_id}: AI-use or paywall notice blocks a summary")
    bullets = summary.get("bullets")
    emphasis = summary.get("emphasis")
    if not (isinstance(bullets, list) and isinstance(emphasis, list)
            and len(bullets) == len(emphasis) == 4):
        raise AssemblyError(f"{article_id}: four bullets and four emphasis phrases required")
    for bullet, phrase in zip(bullets, emphasis, strict=True):
        if not isinstance(bullet, str) or not isinstance(phrase, str) or bullet.count(phrase) != 1:
            raise AssemblyError(f"{article_id}: each emphasis must occur once in its bullet")
    notice = str(summary.get("rights_notice") or "AI 이용 금지 표시 없음")
    # Design Ref: operator request 2026-09-30 — every summary bullet ends with a period.
    bullets = [with_period(bullet) for bullet in bullets]
    return {
        "headline": summary["headline"], "bullets": bullets, "emphasis": emphasis,
        "review": {
            "status": "SOURCE_REVIEWED_RIGHTS_PENDING",
            "basis": "공개 원문 1회 조회 후 요약; 수치·명칭·시점 원문 대조; 사람 검토 전",
            "reviewed_at": reviewed_at,
        },
        "rights": {
            "status": "INTERNAL_ONLY",
            "basis": f"{notice}; 파생 요약의 고객 배포권 미확인; 본인 확인용 내부 검토에 한정",
            "internal_analysis_allowed": True, "checked_at": reviewed_at,
        },
        "evidence": (
            f"원문 조회 {read['retrieved_at_utc']}; HTTP 200; robots 허용; "
            f"원시 바이트 SHA-256 {read['sha256']}; 본문 미보존"
        ),
    }


def assemble(
    candidates_path: Path, review_path: Path, kau_path: Path, *, root: Path = ROOT,
) -> dict[str, Any]:
    candidates_doc = _load(candidates_path)
    review = _load(review_path)
    kau_doc = _load(kau_path)
    issue = date.fromisoformat(str(candidates_doc["issue_date"]))
    cutoff = datetime.fromisoformat(str(candidates_doc["cutoff_at"]))
    window_start = datetime.fromisoformat(str(candidates_doc.get(
        "window_start", (cutoff - timedelta(hours=48)).isoformat())))
    if (cutoff.tzinfo is None or window_start.tzinfo is None
            or window_start >= cutoff):
        raise AssemblyError("candidate publication window is invalid")
    if review.get("issue_date") != issue.isoformat():
        raise AssemblyError("review and candidates are for different issues")
    reviewed_at = str(review["reviewed_at_utc"])
    if datetime.fromisoformat(reviewed_at) > datetime.now(UTC):
        raise AssemblyError("review time is in the future")
    candidates = {
        row["id"]: row for section in candidates_doc["sections"].values()
        for row in section["candidates"]
    }
    collection_path = Path(candidates_doc["collection"]["articles_path"])
    if _sha(collection_path) != candidates_doc["collection"]["articles_sha256"]:
        raise AssemblyError("collection articles changed after candidate ranking")
    collection = {
        str(row.get("article_id")): row
        for row in json.loads(collection_path.read_text(encoding="utf-8"))
    }
    reads = _reads(review)
    summaries: dict[str, Any] = review.get("summaries", {})
    sections: dict[str, Any] = review["sections"]
    unknown = set(sections) - set(SECTIONS)
    if unknown:
        raise AssemblyError(f"unknown sections: {sorted(unknown)}")
    seen: set[str] = set()
    articles: list[dict[str, Any]] = []
    for section in SECTIONS:
        ids = [str(item) for item in sections.get(section, [])]
        if sum(item in summaries for item in ids) > 2:
            raise AssemblyError(f"{section}: at most two summaries")
        for article_id in ids:
            if article_id in seen:
                raise AssemblyError(f"{article_id}: listed twice")
            seen.add(article_id)
            meta = _metadata(article_id, candidates, collection, reads, cutoff, window_start)
            article: dict[str, Any] = {
                "id": article_id, "section": section, "headline": meta["title"],
                "publisher": meta["publisher"], "source_id": meta.get("source_id") or article_id,
                "url": meta["url"], "published_date": meta["published_date"],
                "source_evidence": (
                    f"수집 {candidates_doc['collection']['run_id']} "
                    f"관측 {meta.get('observed_at')}; "
                    f"날짜 근거 {meta.get('date_evidence_url')}"
                )[:600],
                "bullets": [], "emphasis": [],
                "review": {"status": "METADATA_ONLY",
                           "basis": "수집기 제목·발행 메타데이터만 확인; 본문 요약 미작성"},
                "rights": {"status": "INTERNAL_ONLY",
                           "basis": "개인 확인용 기사 링크; 본문 재이용권 미확인",
                           "internal_analysis_allowed": True},
            }
            if meta.get("published_at"):
                article["published_at"] = meta["published_at"]
            if article_id in summaries:
                fields = _summary_fields(
                    article_id, summaries[article_id], reads.get(article_id), reviewed_at
                )
                article["source_headline"] = meta["title"]
                article["headline"] = fields["headline"]
                article["bullets"] = fields["bullets"]
                article["emphasis"] = fields["emphasis"]
                article["review"] = fields["review"]
                article["rights"] = fields["rights"]
                article["source_evidence"] = (
                    article["source_evidence"] + "; " + fields["evidence"]
                )[:600]
            decision = review.get("not_summarized", {}).get(article_id)
            if isinstance(decision, dict):
                article["summary_decision"] = decision
            articles.append(article)
    stray = set(summaries) - seen
    if stray:
        raise AssemblyError(f"summaries without a section: {sorted(stray)}")
    kau = kau_doc.get("kau")
    if not isinstance(kau, dict):
        raise AssemblyError("KAU source content has no kau block")
    notes = [str(note) for note in review.get("editorial_notes", [])]
    notes.append(
        f"KAU 블록은 {_ref(kau_path)['path']}에서 변경 없이 복사함; 최신 거래일 "
        f"{max(str(row.get('trade_date')) for row in kau.get('observations', []))}."
    )
    return {
        "issue_date": issue.isoformat(),
        "cutoff_at": cutoff.isoformat(),
        "window_start": window_start.isoformat(),
        "articles": articles,
        "kau": kau,
        "input_sources": [_ref(candidates_path), _ref(review_path), _ref(kau_path)],
        "collection_run": {"run_id": candidates_doc["collection"]["run_id"],
                           "path": str(collection_path),
                           "sha256": candidates_doc["collection"]["articles_sha256"]},
        "editorial_notes": notes,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidates", required=True, type=Path)
    parser.add_argument("--review", required=True, type=Path)
    parser.add_argument("--kau-content", type=Path,
                        help="Content file whose KAU block is reused; default newest v7 input")
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("output exists; choose a new version")
    try:
        issue = date.fromisoformat(_load(args.candidates)["issue_date"])
        kau_path = args.kau_content or latest_kau_content(ROOT, issue)
        result = assemble(args.candidates, args.review, kau_path)
    except (AssemblyError, OSError, ValueError, KeyError) as exc:
        parser.error(str(exc))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as handle:
        handle.write(json.dumps(result, ensure_ascii=False, indent=1) + "\n")
    counts = {name: sum(a["section"] == name for a in result["articles"]) for name in SECTIONS}
    summaries = sum(bool(a["bullets"]) for a in result["articles"])
    print(json.dumps({"output": str(args.output), "articles": len(result["articles"]),
                      "summaries": summaries, "sections": counts}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
