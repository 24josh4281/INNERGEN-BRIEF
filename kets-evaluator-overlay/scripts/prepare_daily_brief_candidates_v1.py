"""Rank one day's collected article metadata into the eight daily-brief sections.

Design Ref: operator request 2026-09-30 for a 09:00 daily brief with one page per
section. This command is offline: it reads the Carbon Issue Tracker collection and
earlier brief manifests, proposes a selection and never fetches, summarizes or
approves an article. Rights hints are only hints; the page notice decides later.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import re
from collections.abc import Callable, Iterable, Mapping
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
TRACKER_ROOT = Path(r"B:\CODEX\Carbon Issue Tracker")
RIGHTS_CONFIG = ROOT / "config/daily_brief_publisher_rights_v1.json"
KST = ZoneInfo("Asia/Seoul")
SECTIONS: tuple[tuple[str, int], ...] = (
    ("국내 정책", 3), ("해외 정책", 3), ("국내 산업", 3), ("해외 산업", 3),
    ("자발적·국제 탄소시장", 3), ("재생에너지", 3), ("기후 공시", 3), ("기타", 10),
)
TRACKER_SECTION = {
    "국내 정책": "국내 정책", "해외 정책": "해외 정책", "국내 산업": "국내 산업",
    "해외 산업": "해외 산업", "자발적 배출권 및 국제 탄소시장": "자발적·국제 탄소시장",
    "재생에너지": "재생에너지",
}
DISCLOSURE = re.compile(
    r"(?:지속가능성|ESG|기후|탄소|환경|배출량)\S*\s*(?:정보\s*)?공시|공시\s*(?:기준|의무|로드맵)|"
    r"ISSB|KSSB|CSRD|ESRS|TCFD|CSDDD|지속가능성\s*(?:보고|인증|기준)|ISSA\s*5000|"
    r"SBTi|CDP|그린워싱|greenwash|climate disclosure|sustainability reporting|assurance",
    re.IGNORECASE,
)
DOMESTIC_POLICY = re.compile(
    r"^(?:정부|기후부|기후에너지환경부|환경부|산업부|산업통상부|국토부|금융위|금융당국|국회|"
    r"기재부|기획예산처|국무조정실)|법안|시행령|개정안|의무화|로드맵|국정감사|국감"
)
FOREIGN_POLICY = re.compile(
    r"(?:EU|유럽|미국|美|중국|中|일본|日|영국|호주|캐나다|UN|UNFCCC|COP\d*)"
    r".{0,20}(?:규제|규정|법|정책|의무|의회|위원회|정부|행정부|연기|도입|시행)"
)
DOMESTIC_HINT = re.compile(r"한국|국내|정부|기후부|산업부|국회|K-?ETS|배출권거래제")
PRIORITY = {"A": 0, "B": 1, "C": 2}
TIER = {"carbon_core": 0, "esg": 1, "transition": 2, "source_context": 3, "industry": 4}
TIMED = {"second", "minute"}
SUMMARY_BLOCKS = {"AI_USE_PROHIBITED", "PAYWALL_LIKELY"}


class CandidateError(ValueError):
    """The collection or earlier briefs cannot support a safe candidate list."""


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_classifier(tracker_root: Path) -> Callable[[Mapping[str, Any]], dict[str, Any]]:
    """Reuse the tracker's title rules without copying or editing them."""
    path = tracker_root / "tools/editorial_sections.py"
    spec = importlib.util.spec_from_file_location("tracker_editorial_sections", path)
    if spec is None or spec.loader is None:
        raise CandidateError(f"tracker classifier is missing: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    classify: Callable[[Mapping[str, Any]], dict[str, Any]] = module.classify_section
    return classify


def section_for(row: Mapping[str, Any], classify: Callable[[Mapping[str, Any]], dict[str, Any]]
                ) -> tuple[str, str]:
    """Map the tracker's six sections to eight; disclosure titles form their own page."""
    result = classify(row)
    tracker = str(result.get("section"))
    title = str(row.get("title", ""))
    if tracker != "자발적 배출권 및 국제 탄소시장" and DISCLOSURE.search(title):
        return "기후 공시", "제목의 공시·인증 단서"
    if tracker in TRACKER_SECTION:
        return TRACKER_SECTION[tracker], str(result.get("basis", ""))
    if FOREIGN_POLICY.search(title):
        return "해외 정책", "추적기 분류 확인 대상; 제목의 해외 정부·규제 단서"
    if DOMESTIC_POLICY.search(title) and DOMESTIC_HINT.search(title):
        return "국내 정책", "추적기 분류 확인 대상; 제목의 국내 정부·입법 단서"
    return "기타", "추적기 분류 확인 대상; 탄소·에너지 관련 우선순위 후보"


def rights_hint(publisher: str, config: Mapping[str, Any]) -> dict[str, str]:
    for entry in config.get("publishers", []):
        if any(name in publisher for name in entry["match"]):
            return {"hint": entry["hint"], "evidence": entry["evidence"]}
    return {"hint": "CHECK_ON_READ", "evidence": "원문을 읽을 때 권리 표시 확인"}


def _plain_id(value: str) -> str:
    return value.removeprefix("CIT-")


def published_before(root: Path, issue: date) -> set[str]:
    """IDs and URLs already shown in an earlier issue are not repeated."""
    seen: set[str] = set()
    for path in (root / "output/newsletter").glob("**/*.manifest.json"):
        try:
            manifest = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValueError):
            continue
        if not isinstance(manifest, dict) or not isinstance(manifest.get("issue_date"), str):
            continue
        try:
            earlier = date.fromisoformat(manifest["issue_date"]) < issue
        except ValueError:
            continue
        ids = manifest.get("included_article_ids")
        if not earlier or not isinstance(ids, list):
            continue
        seen.update(_plain_id(str(item)) for item in ids)
        content = Path(str(manifest.get("input_path", "")))
        if content.is_file():
            try:
                articles = json.loads(content.read_text(encoding="utf-8")).get("articles", [])
            except (OSError, UnicodeError, ValueError, AttributeError):
                articles = []
            seen.update(str(item.get("url")) for item in articles if isinstance(item, dict))
    return seen


def _https(row: Mapping[str, Any]) -> str | None:
    for key in ("canonical_url", "url"):
        value = row.get(key)
        if isinstance(value, str):
            parts = urlsplit(value)
            if parts.scheme == "https" and parts.hostname and " " not in value:
                return value
    return None


def _normal_title(title: str) -> str:
    return re.sub(r"[\W_]+", "", title).lower()


def _date_only(row: Mapping[str, Any]) -> date | None:
    raw = str(row.get("published_at_raw") or "")
    if row.get("date_precision") == "day" and re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
        return date.fromisoformat(raw)
    return None


def _moment(row: Mapping[str, Any]) -> datetime:
    stamp = row.get("published_at_kst")
    if isinstance(stamp, str) and stamp and row.get("date_precision") in TIMED:
        return datetime.fromisoformat(stamp)
    day = _date_only(row)
    if day is None:
        raise CandidateError(f"{row.get('article_id')}: publication date is missing")
    return datetime(day.year, day.month, day.day, 12, tzinfo=KST)


def rank_key(row: Mapping[str, Any]) -> tuple[int, int, int, float]:
    official = 0 if row.get("transport") in {"official_feed", "official_list"} else 1
    return (PRIORITY.get(str(row.get("priority")), 3), official,
            TIER.get(str(row.get("topic_tier")), 5), -_moment(row).timestamp())


def eligible_rows(
    rows: Iterable[Mapping[str, Any]], cutoff: datetime, excluded: set[str],
    window_start: datetime | None = None,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Keep timed, HTTPS, in-window, unseen and non-duplicate candidates."""
    lower = window_start or cutoff - timedelta(hours=48)
    skipped: dict[str, int] = {}
    kept: list[dict[str, Any]] = []
    for raw in rows:
        reason = None
        url = _https(raw)
        stamp = raw.get("published_at_kst")
        day = _date_only(raw)
        if (_plain_id(str(raw.get("article_id"))) in excluded
                or (url is not None and url in excluded)):
            reason = "earlier_issue"
        elif raw.get("date_status") != "in_window":
            reason = "date_not_in_window"
        elif url is None:
            reason = "not_https"
        elif raw.get("date_precision") in TIMED and isinstance(stamp, str) and stamp:
            if not lower <= datetime.fromisoformat(stamp) < cutoff:
                reason = ("outside_publication_window" if window_start is not None
                          else "outside_48_hours")
        elif day is not None:
            # A date-only item must fall wholly inside the window; boundary days
            # and the issue day need a clock time the source did not give.
            if not lower.date() < day < cutoff.date():
                reason = "date_only_on_boundary"
        else:
            reason = "no_publication_date"
        if reason:
            skipped[reason] = skipped.get(reason, 0) + 1
            continue
        kept.append({**raw, "brief_url": url})
    kept.sort(key=rank_key)
    unique: list[dict[str, Any]] = []
    groups: set[str] = set()
    titles: set[str] = set()
    for row in kept:
        group = str(row.get("similarity_group_id") or "")
        title = _normal_title(str(row.get("title", "")))
        if (group and group in groups) or title in titles:
            skipped["duplicate"] = skipped.get("duplicate", 0) + 1
            continue
        if group:
            groups.add(group)
        titles.add(title)
        unique.append(row)
    return unique, skipped


def select_section(candidates: list[dict[str, Any]], target: int) -> tuple[list[str], list[str]]:
    """Put two summary-eligible items first, then fill by rank."""
    eligible = [row["id"] for row in candidates if row["rights_hint"] not in SUMMARY_BLOCKS]
    summaries = eligible[:2]
    chosen = list(summaries)
    for row in candidates:
        if len(chosen) >= target:
            break
        if row["id"] not in chosen:
            chosen.append(row["id"])
    order = {row["id"]: index for index, row in enumerate(candidates)}
    return sorted(chosen, key=lambda item: (item not in summaries, order[item])), summaries


def build_candidates(
    issue: date, cutoff: datetime, run_dir: Path, *, root: Path = ROOT,
    tracker_root: Path = TRACKER_ROOT, per_section: int = 8,
    window_start: datetime | None = None,
) -> dict[str, Any]:
    articles_path = run_dir / "articles.json"
    report_path = run_dir / "run_report.json"
    if not articles_path.is_file() or not report_path.is_file():
        raise CandidateError(f"collection run is incomplete: {run_dir}")
    report = json.loads(report_path.read_text(encoding="utf-8"))
    rows = json.loads(articles_path.read_text(encoding="utf-8"))
    if not isinstance(rows, list):
        raise CandidateError("collection articles must be a JSON array")
    config = json.loads(RIGHTS_CONFIG.read_text(encoding="utf-8"))
    classify = load_classifier(tracker_root)
    excluded = published_before(root, issue)
    lower = window_start or cutoff - timedelta(hours=48)
    if lower.tzinfo is None or lower.utcoffset() is None or lower >= cutoff:
        raise CandidateError("window start must be timezone-aware and before cutoff")
    # Plan SC: a 21:00 edition uses only the confirmed 09:00-21:00 publication window.
    if window_start is not None:
        collected = report.get("window", {})
        try:
            collection_start = datetime.fromisoformat(collected["start"])
            collection_end = datetime.fromisoformat(collected["end"])
        except (KeyError, TypeError, ValueError) as exc:
            raise CandidateError("collection window is missing or invalid") from exc
        if collection_start > lower or collection_end < cutoff:
            raise CandidateError("collection run does not cover the requested brief window")
    unique, skipped = eligible_rows(rows, cutoff, excluded, window_start)
    sections: dict[str, dict[str, Any]] = {}
    pools: dict[str, list[dict[str, Any]]] = {name: [] for name, _ in SECTIONS}
    for row in unique:
        name, basis = section_for(row, classify)
        day = _date_only(row) if row.get("date_precision") not in TIMED else None
        published = _moment(row).astimezone(KST)
        hint = rights_hint(str(row.get("publisher", "")), config)
        pools[name].append({
            "id": row["article_id"], "section": name, "title": row.get("title", ""),
            "publisher": row.get("publisher", ""), "url": row["brief_url"],
            "published_at": None if day else published.isoformat(),
            "published_date": (day or published.date()).isoformat(),
            "official": row.get("transport") in {"official_feed", "official_list"},
            "source_id": row.get("source_id", ""), "priority": row.get("priority"),
            "topic_tier": row.get("topic_tier"), "section_basis": basis,
            "rights_hint": hint["hint"], "rights_hint_evidence": hint["evidence"],
            "observed_at": row.get("observed_at"),
            "date_evidence_url": row.get("date_evidence_url"),
        })
    for name, target in SECTIONS:
        candidates = pools[name][:per_section + target]
        selected, summaries = select_section(candidates, target)
        sections[name] = {"target": target, "selected": selected,
                          "suggested_summaries": summaries, "candidates": candidates}
    return {
        "schema_version": 1,
        "purpose": "personal_daily_brief_candidates_not_approval",
        "issue_date": issue.isoformat(),
        "cutoff_at": cutoff.isoformat(),
        "window_start": lower.isoformat(),
        "collection": {
            "run_id": run_dir.name, "status": report.get("status", report.get("mode")),
            "articles_path": str(articles_path), "articles_sha256": _sha(articles_path),
            "report_path": str(report_path), "report_sha256": _sha(report_path),
        },
        "rights_config": {"path": str(RIGHTS_CONFIG.relative_to(ROOT)),
                          "sha256": _sha(RIGHTS_CONFIG)},
        "earlier_issue_ids_excluded": len(excluded),
        "skipped": skipped,
        "sections": sections,
    }


def markdown(result: Mapping[str, Any]) -> str:
    lines = [f"# {result['issue_date']} 일간 브리프 후보", "",
             f"기준시각 {result['cutoff_at']} · 수집 {result['collection']['run_id']}", ""]
    for name, section in result["sections"].items():
        lines.extend((f"## {name} (목표 {section['target']}건)", ""))
        for row in section["candidates"]:
            mark = ("요약 제안" if row["id"] in section["suggested_summaries"]
                    else "선정" if row["id"] in section["selected"] else "대기")
            lines.append(
                f"- [{mark}] {row['title']} · {row['publisher']} · "
                f"{(row['published_at'] or row['published_date'])[:16]} · "
                f"{row['priority']}/{row['topic_tier']} · "
                f"권리 힌트 {row['rights_hint']} · `{row['id']}`"
            )
        lines.append("")
    return "\n".join(lines)


def latest_run(tracker_root: Path) -> Path:
    pointer_path = tracker_root / "output/collection/latest.json"
    pointer = json.loads(pointer_path.read_text(encoding="utf-8"))
    if pointer.get("status") != "ok":
        raise CandidateError("latest tracker collection did not finish ok")
    return Path(pointer["run_dir"])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--issue-date", type=date.fromisoformat,
                        default=datetime.now(KST).date())
    parser.add_argument("--cutoff", help="ISO time; default is 09:00 KST on the issue date")
    parser.add_argument("--window-start", help="ISO time; 09:00 KST for a 21:00 evening issue")
    parser.add_argument("--run-dir", type=Path, help="Tracker collection run; default latest")
    parser.add_argument("--tracker-root", type=Path, default=TRACKER_ROOT)
    parser.add_argument("--output", type=Path, required=True, help="New JSON path")
    args = parser.parse_args()
    issue: date = args.issue_date
    cutoff = (datetime.fromisoformat(args.cutoff) if args.cutoff
              else datetime(issue.year, issue.month, issue.day, 9, tzinfo=KST))
    if cutoff.tzinfo is None or cutoff.astimezone(KST).date() != issue:
        parser.error("cutoff must be timezone-aware and on the issue date")
    window_start = datetime.fromisoformat(args.window_start) if args.window_start else None
    if window_start is not None and (
        window_start.tzinfo is None or window_start.utcoffset() is None
        or window_start.astimezone(KST).date() != issue
    ):
        parser.error("window start must be timezone-aware and on the issue date")
    output: Path = args.output
    if output.exists() or output.with_suffix(".md").exists():
        parser.error("output exists; choose a new version")
    run_dir = args.run_dir or latest_run(args.tracker_root)
    try:
        result = build_candidates(issue, cutoff, run_dir, tracker_root=args.tracker_root,
                                  window_start=window_start)
    except (CandidateError, OSError, ValueError) as exc:
        parser.error(str(exc))
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("x", encoding="utf-8") as handle:
        handle.write(json.dumps(result, ensure_ascii=False, indent=1) + "\n")
    output.with_suffix(".md").write_text(markdown(result), encoding="utf-8")
    counts = {name: len(section["candidates"]) for name, section in result["sections"].items()}
    print(json.dumps({"output": str(output), "candidates": counts,
                      "skipped": result["skipped"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
