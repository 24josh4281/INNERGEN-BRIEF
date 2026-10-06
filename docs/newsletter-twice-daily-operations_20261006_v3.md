# 일간 브리프 오전·저녁 운영 v3 (2026-10-06)

모든 기준시각은 Asia/Seoul이다. 오전 09:00판과 저녁 21:00판은 서로 다른 발행 구간과 발송 장부 항목을 사용한다. [v2](newsletter-twice-daily-operations_20261005_v2.md)는 저녁 PDF·PPT 개인 브리프를 만들지 않던 이전 운영안으로 보존한다.

| 판본 | 기사 발행 구간 | 결과 | 개인 메일 |
| --- | --- | --- | --- |
| 오전 09:00 | 09:00 미만. 기존 오전 수집·원문 검토 조건 유지 | 후보 메일, 4불릿 브리프, 개인 PDF·PPT 일간 브리프 | 각각 기존 중복 점검 후 최대 1회 |
| 저녁 21:00 | 당일 09:00 이상~21:00 미만. 시각이 확인되지 않는 당일 기사는 제외 | 후보 메일, **별도의 개인 PDF·PPT 일간 브리프** | 별도 `YYYY-MM-DD 21:00` 장부 항목으로 최대 1회 |

저녁 개인 브리프는 당일 밤 회사 메일에 접수해 다음 날 오전에 확인하는 시험 운영이다. 21:00은 **수집과 편집의 시작 및 기사 발행 제외 경계**이며 메일 수신 완료 시각을 보장하지 않는다. 실제 수집 완료, 원문 확인, PDF·PPT 생성, SMTP 접수, 받은편지함 도착은 따로 기록한다. Codex 앱 또는 PC가 꺼져 있거나 원문·권리·SMTP에 문제가 있으면 지연 또는 보류될 수 있다. 다음 날 오전 예약은 전날 저녁판의 접수 영수증을 확인하고 누락 시 즉시 알리되, 전날 날짜의 메일을 무단 재발송하지 않는다.

## 저녁 개인 브리프 실행 순서

1. K-ETS Evaluator의 `AGENTS.md`, `docs/knowledge/innergen-kau-interpretation_20260928_v2.md`, `docs/kau-indicator-collection-guide.md`, `docs/kau-writing-style.md`, 기존 개인 브리프 운영 문서를 읽는다. 고객 배포판이 아닌 **본인 확인용 내부 검토본**으로 취급한다.
2. `scripts/daily_brief_status_v1.py --date YYYY-MM-DD --edition evening`으로 `DONE`, `RUNNING`, `STOP_UNCERTAIN`, `WORK`를 확인한다. `WORK`에서만 `--claim 21:00 --edition evening`으로 별도 폴더 잠금을 잡는다. 성공한 수집·메일은 다시 실행하지 않는다.
3. Carbon Issue Tracker에서 당일 09:00 이상~21:00 미만 live 수집의 run ID, `run_report.json` 구간과 완료시각, 기사 원본 해시를 확인한다. 수집이 완료되지 않았다면 v2의 15분 간격 복구 규칙을 따른다. 권리 차단·로그인·CAPTCHA·API 키 부재·KRX 하루 1회 제한은 반복 요청하지 않는다.
4. `prepare_daily_brief_candidates_v1.py --issue-date YYYY-MM-DD --cutoff YYYY-MM-DDT21:00:00+09:00 --window-start YYYY-MM-DDT09:00:00+09:00 --run-dir <저녁 수집> --output output/newsletter/daily_brief_YYYYMMDD_evening/candidates_YYYYMMDD_v1.json`으로 후보를 만든다. 시간 표시가 없는 당일 기사는 구간을 입증할 수 없으므로 제외한다.
5. 공개 원문을 허용된 방식으로 확인한다. 권리 금지·유료벽·원문 미확인 기사는 요약하지 않고 제목·링크로만 둔다. `review_YYYYMMDD_v1.json`에 원문 시각·해시·권리 표시·편집 판단을 남긴다. `assemble_daily_brief_content_v1.py`로 입력을 만들고 KAU 블록은 검증된 기존 값을 기준 거래일·단위와 함께 그대로 사용한다. 해외 가격 블록은 당일 자료의 기준일, 통화, 단위 및 권리 상태를 구분한다. 없는 가격이나 숫자를 채우지 않는다.
6. `build_daily_self_review_brief_v9.py`로 `--version self_review_evening_v1`의 PDF·PPT 및 매니페스트를 새 파일로 만든다. 페이지 수, 링크, 기사 발행 시각, 원문 요약 근거, 수치·단위, 원본 SHA-256을 대조한다.
7. `send_personal_daily_brief_v4.py --brief-manifest <저녁 manifest> --output output/newsletter/personal_mail_evening_YYYYMMDD_v1 --edition evening`으로 초안을 만든다. 제목은 `... | YYYY-MM-DD | 21:00 저녁판`, 첨부 이름은 `_2100`으로 구분한다. 메일의 발신·수신 주소가 모두 `sejinkim@inng.co.kr`이며 PDF·PPT 바이트가 매니페스트 해시와 같은지 확인한 뒤 `--prepared <저녁 초안 폴더> --send`를 **한 번** 실행한다. `--allow-resend`는 사용하지 않는다.
8. `delivery_receipt.json` 및 SMTP 장부의 `YYYY-MM-DD 21:00` 항목을 확인하고 잠금을 해제한다. `SENT`는 SMTP 접수일 뿐 받은편지함 도착이 아니다. 접수 여부가 불명확하면 재발송하지 않고 조사해 즉시 알린다.

## 다음 날 오전 확인

오전 통합 예약은 전날 21:00판의 `personal_mail_evening_YYYYMMDD_*/delivery_receipt.json`과 발송 장부를 확인한다. 영수증이 없거나 `STOP_UNCERTAIN`이면 수집·원문·권리·편집·메일 가운데 멈춘 단계를 진단해 사용자에게 실제 상태와 필요한 조치를 알린다. 이미 SMTP에 접수된 판본은 다시 보내지 않는다.
