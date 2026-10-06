# K-ETS Evaluator 저녁 브리프 변경분

이 폴더의 `scripts/`와 `tests/unit/` 파일은 기존 K-ETS Evaluator Python 3.12 작업 폴더에 적용하는 **변경 파일 묶음**이다. 독립 실행 프로그램은 아니다. 기존 프로젝트의 `src/kets_intelligence/`, 나머지 브리프 스크립트, 로컬 `.venv`가 필요하다.

## 적용

1. 기존 같은 이름의 파일을 별도로 보존한다.
2. 이 폴더의 파일을 K-ETS Evaluator의 같은 상대 경로에 복사한다.
3. 프로젝트에서 `./scripts/check.ps1`을 실행한다. 2026-10-06 작업 폴더에서는 오프라인 테스트 1,200개와 Ruff·mypy·구문 검사가 통과했다.
4. `docs/newsletter-twice-daily-operations_20261006_v3.md`의 21:00 제작·발송 절차에 맞춰 예약을 설정한다. 예약 설정 자체와 회사 메일 인증 정보는 이 저장소에 들어 있지 않다.

`--edition evening`은 같은 날짜의 오전판과 별도의 `YYYY-MM-DD 21:00` 발송 장부 항목을 사용한다. 수집·원문·권리 검토 없이 메일을 보내는 기능이 아니다. 발송은 본인 회사 메일로만 허용되며, SMTP 접수와 받은편지함 도착을 구분한다.
