# 에이전트 공용 기록

작성일: 2026-10-20. 웹 담당과 통합 담당의 설계 차이·의존성·공유 정보를 관리하는 단일 기록 파일이다. 작업 상태는 사실대로 적으며 메시지 전송/알림/상대의 확인을 이 파일만으로 보장하지 않는다.

## 읽기와 갱신 규칙

- 작업 시작/재개, 계약 사용 전, PR 생성/병합 전에 최신 develop의 이 파일과 [웹 설계도](web-agent-blueprint.md)를 읽는다. 별도 worktree/브랜치의 파일은 자동 동기화되지 않는다.
- 중요한 요청/계약 변경은 문서 PR로 먼저 공유하고 상대가 읽은 기준 SHA/계약을 자기 기록에 명시한다. 로컬에만 작성하고 전달이 끝났다고 보고하지 않는다. 새 사용자 결정·외부 수집/비용은 기존 승인 규칙을 따른다.
- 통합 담당은 확정 계약/소유권/통합 상태 표를 관리한다. 웹 담당은 자기 WEB-* 항목만 추가/갱신하고, 통합 담당은 INT-*로 응답한다. 상대 항목을 삭제하거나 상대가 동의/검증했다고 대신 적지 않는다.
- 상태는 제안 → 확인 → 구현 → 검증 → 병합으로 구분한다. 차단·보류·철회도 원인을 남긴다. 확인은 기술적 수신/영향 확인이며 사용자 승인이나 구현/병합 완료를 뜻하지 않는다.
- 설계 변경은 변경 전/후·이유·영향·호환성·결정 근거·대상 버전·담당·검증·PR/commit을 남긴다. 병합 후에도 과거 결정을 지우지 않고 대체 기록 ID를 연결한다.
- 같은 파일 충돌은 양쪽 항목을 보존해 해결한다. ours/theirs로 전체 파일을 덮어쓰지 않는다. 실제 기준 설계도 갱신은 통합 담당이 같은 PR에서 처리한다.
- 비밀값·실데이터·대화 전문·원본 응답·모델 파일은 적지 않는다. 필요한 최소 합성 재현만 남긴다.

## 현재 기준

| 항목 | 상태/소유자 |
|---|---|
| 이 문서 작성 시작 기준 | develop ec6ff24, 강화 설계 PR #55 병합 결과. 이후 작업자는 실제 최신 develop을 다시 확인 |
| 사용자 확정 스택 | Vue 3 + Vite + TypeScript 독립 프론트엔드 |
| 사용자 확정 분담 | 웹: 화면·통신 클라이언트·모의 테스트. 통합: 엔진·Agent·API·실제 연결 |
| 현재 작업 | W6 평가 코드·합성 검증25/25(INT-029). 실제 모델 지표 미측정. 마감10/22 13:00 KST |
| 통합 담당 | 현재 대화. 엔진/Agent/API와 공유 계약 및 PR 병합 전담 |
| 웹 담당 | 별도 담당의 웹 모의 앱 PR #61 병합. 사용 기준/도구/제안은 WEB-001/002 유지 |
| 웹 계약 | web-agent.v0 구현/검증: agent-http-sse-contract.md·INT-022~025. 실제 브라우저3사례 확인·검증용 서버 종료 |
| 실제 기존 API | GET /health, GET /cards/{spid}, POST /recommend/position. 채팅/스쿼드 스트림 아님 |
| 보호 경로 | 웹은 frontend/ 웹 파일과 자기 WEB-* 기록만 수정. frontend/AGENTS.md·FRS/·기존 tests/·공통 설정 등은 읽기 전용 |
| 검증 기준 | Python 필수 검사 유지. W5#84 통합 기준Python1,492·웹 Node22/타입·108테스트·빌드 통과. 웹CI 미구축. W6 문서에서 새로 실행한 수치가 아님 |
| 구현된 자동 보호 | Python CI만 존재. 경로 자동 차단/웹 CI·파일 권한 격리는 미구축 |
| 데이터/실행 | 합성 Agent MVP 우선. 1-D/1-F 미완료, 새 수집·실모델 실행/비용·외부 tracing 승인 확대 없음 |

## 계약·환경 확인 대기

| ID | 공유할 정보/의존성 | 담당 | 다음 조치 |
|---|---|---|---|
| INT-001 | web-agent.v0 HTTP/SSE wire | 통합 | web-agent.v0 DTO/HTTP·실제 모델 ASGI·브라우저3사례 통과(INT-022~025). 기능 기준 develop00623baa·지속 서버 없음 |
| WEB-001 | Node/npm·Vue/Vite/TypeScript·테스트 도구 버전과 설치/검증 명령 | 웹 | 구현. 버전 고정·명령은 아래 WEB-001과 frontend/README. 웹 CI 추가는 INT-004 대기 |
| INT-002 | 경로·주소/origin·CORS·실행/준비 상태 | 통합 | 별도 합성 앱/8001·origin2개/readiness·임시 실제 연결 검증(INT-023~025). 검증용 서버 종료 |
| INT-003 | 동시 실행·중복·취소·시간 상한/재시도 정책 | 통합 | 동시1/대기0/초과409·150초/보수적 종료 구현(INT-023). ID 상관관계만·중복 억제 없음·재시도0 |
| INT-004 | mock/live 계약 검사·보호 경로 확인·웹 CI 구성 | 통합 | 웹 도구/스크립트 확정 후 별도 구현/검증. 현재 자동 차단 없음 |
| INT-005 | 실제 합성 Agent 연결과 최종 만료/설명 검사 | 통합 | W1~W4 백엔드 구현 후 엔드포인트·PR·SHA·합성 대조 결과 공유 |

위 표의 ID는 추적 키다. 표에 항목이 있다고 요청이 확인/실행된 것은 아니다. 상세 갱신은 아래 기록에 남긴다.

## 진행·변경·질문 기록

### INT-029 — W6 평가 코드·합성 대역 검증 (2026-10-21)

- 통합 / 사용자 승인·구현·합성 검증. 문서#86은 최종head20cf4dd/CI39519844754 성공 뒤developc0d2f04로 Squash 병합·메시지/트리/완료 브랜치 로컬·원격 정리 확인. 현재feat/offline-agent-evaluation은c0d2f04 기준이다.
- 사용자 선택: 외부 요청 없이25문장 평가 실행기·합성 테스트·로컬 보고서 생성. FRS/evaluation의 고정 JSON/문서 SHA·독립 근거·정답ID 선택 대역·사전11명/양수하한 증명·기존 그래프/엔진/출력 투영·독립 채점/보고서 구현. 기존 core/agent/api/mcp·웹/WEB-*·의존성·CI 변경 없음.
- 최종w6-offline-20261021-003:대역25/25·선택16/16·정상 유효11명8/8·해없음3/3·준비차단9/9. 초기001의 급여0 구간 선택 오류24/25와002는 보존했고 원문/정답은 바꾸지 않았다. 모델/HTTP/넥슨/업로드0·토큰/실제 모델 의미·지연 미측정.
- 신규61/전체pytest1,553개·Ruff·환경/pip 통과. 표/조건/상태/기한 변조·선택 실패·만료·중단/미실행 고정 분모·N/A·덮어쓰기 금지 검증. 문서/원본 해시·명시 시각/자료·제한은stage-w6-evaluation-implementation.md.
- 웹 wire/화면 변경 요청 없음·상대 수신/검증 주장 없음. 마감10/22 13:00 KST·발표 시각 미정/실데이터1-D/F 미완료 유지. 최신head CI/develop Squash·완료 브랜치 정리·기능묶음master PR은 실제 결과를 확인한다. 실모델 평가/호출은 별도 범위다.

### INT-028 — W6 사전 정답·지표·실제 마감 (2026-10-21)

- 통합 / 사용자 결정·문서 고정. 기준develop83c7eed(#84)/docs/agent-evaluation-contract. W5 최종head0f51b5b/CI39518543150 성공 후83c7eed로 Squash 병합·메시지/트리 확인·완료 브랜치 로컬/원격 정리를 마쳤다. master 대상#85는 다른 실행 주체가2026-10-21 09:08:43 KST에7bcdf9a566a1488867202d4f743d523959e2ecc3로 병합한 사실을 확인했다. 이번 대화에서 master 병합을 실행하지 않았다.
- 사용자 답변: 정상8/해없음3/미지원5/필수누락4/모호·금액·정정5의25문장·사전 정답·지표 계약부터 문서화. 통과율 목표90%/85%는 보류. “내일 오후1시”는2026-10-22 13:00 KST 완료·제출 준비 마감이며 실제 발표 시각/외부 제출 절차는 미확정이다.
- stage-w6-evaluation-cases.md·stage-w6-evaluation-contract.md에 정답5조건·구간/사유·차단 단계·정적 합성 자료 경계·분모/미실행/대역 구분·후속 일정 고정. 준비 차단9개/선택 대상16개는 산정이며 새 호출 승인이 아니다.
- 영향은문서/설계 결정·검증 로그·인수인계뿐. 엔진/Agent/API/웹/WEB-*·종료 실행·사용자 파일 보존. 웹 wire/화면 변경 요청과 상대 수신 주장 없음. 이번 평가 코드/실제 모델·메타데이터/엔진/자료 생성/외부 업로드0.
- 검증:25개 고유ID·그룹 수·내용/링크·diff/보호 경로 확인. 로컬 기능 테스트 미실행(문서만). 문서 PR 최신CI/병합 결과는 완료 후 확인한다. 다음 구현/실제 호출 범위는 별도이며 발표 시각도 미정이다.

### INT-027 — W5 로컬 stdio 구현·합성 대조 (2026-10-21)

- 통합 / 구현·합성 검증. 계약#82는 head7abfd24/CI39516249397 성공 후develop45d518a로 Squash 병합·메시지/트리 확인·완료 브랜치 정리. feat/local-mcp-tools는 최신 계약을 반영했다. #81은 별도 실행 주체가 masterf409db5로 병합한 사실을 확인했고 이 대화에서 master 병합을 실행하지 않았다.
- FRS/mcp의 엄격 mcp-build.v1·ID/원문·고정 합성 context·Tool1/Resource/Prompt·단일 worker/lease·stdio CLI/단일 전송 client를 구현했다. 기존 HTTP/core/graph/presentation·웹/WEB-* 보존, 환경 import 목록과 고정 SDK/전이 의존성만 추가했다. 이후 upstream develop8e4dee9/웹#83을 그대로 병합하고 Node22 타입/웹108테스트/빌드·Python1,492개를 통합 상태로 재검증했다.
- 신규52/전체pytest1,492·Ruff/환경/pip 통과. 실제stdio12개(정상9·누락/충돌2·미지원1)와 직접 결과 전체 일치·프로토콜2026-07-28·subprocess EOF 정상 종료. 취소/실제종료까지busy·마지막 만료·큰수/변조·SDK HEADER_MISMATCH 재전송 없음 확인. stage-w5-mcp-implementation.md에 실행/해시/한계를 기록했다.
- 모델/HTTP/넥슨·호스트 등록/지속서버/외부tracing0. Prompt는 후보 제공이며 실제 LLM tool-use가 아니다. 웹은 기존 web-agent.v0 HTTP 그대로이며 주소/화면 변경 요청 없음. 실제 모델 정확도/일반 MCP host/운영 SLA·W6 완료 주장 없음.
- 최신 기능 PR CI/develop Squash/완료 브랜치 정리·master 기능 묶음 PR은 실제 결과를 확인한다. 상대 수신/검증을 대신 주장하지 않는다.

### INT-026 — W5 로컬 MCP 계약 확정 (2026-10-21)

- 통합 / 계약·사용자 결정. 기준 developc0c47bef(#80), docs/mcp-integration-contract. #80은 headdebdacd/CI39514966847 성공 후c0c47bef로 Squash 병합·표기/트리 확인·완료 브랜치 정리했고 기능 묶음#81 생성까지 완료했다.
- 사용자 승인: stdio·공식SDK2.3.0 호환/고정·build_squad Tool/지원 Resource/후보 선택 Prompt·합성 직접/MCP 대조. 원문+candidate-selection.v1→서버 registry검증/고정 context→mcp-build.v1, 동시1/대기0/150초/재시도·직접fallback0/실제 작업 종료까지 슬롯·ID상관관계만.
- 이번 문서 PR은 계약/최신 로드맵 정리다. SDK 설치/서버/실제 프로토콜 검증 완료가 아니다. 다음 별도 기능 PR에서 기존 엔진/그래프를 재사용하며 HTTP/웹/WEB-*·실데이터/과거실행은 보존한다.
- 실제 모델/호스트 등록/HTTP MCP/배포/외부 tracing/새 수집 제외. 현재 웹 wire나 실제 연결 주소 변경 요청 없음. 공식SDK v2/배포 metadata 확인 근거와 잠금/취소 검증 계획은 stage-w5-mcp-contract.md.

### INT-025 — 실제 브라우저3사례·임시 서버 종료 (2026-10-21)

- 통합 / 실제 소량 검증. API#79는 head09b744a/CI39514039395 성공 후develop00623baa로 Squash 병합·원문/트리 확인·완료 브랜치 로컬/원격 삭제. 웹 PR#75/#78 포함·WEB-004 계약 유지. 웹/WEB-*·엔진/Agent 소스 수정 없음.
- 사용자 승인001은 상태1회 연결 거부·추론0/브라우저0에서 중단. 이후 Ollama 시작/새002를 별도 승인받아 상태2/추론3·실제 브라우저POST3회 완료, 원래001/잠금 보존·재시도0. Node22.23.3/기존 lock npm ci·타입/87테스트/빌드 통과.
- web-agent.v0/실제 연결127.0.0.1:5173→8001→qwen3:8b/명시 digest·새 합성 배치·시각에서 정상optimal11명·누락/충돌명확화·이전 표 제거를 확인했다. 실제 SSE 무호출3/3 검사와 실행 중 AX/PNG 수동 화면 확인을 구분한다. AX 저장본은 diff뿐인 한계도 agent-browser-e2e.md에 기록했다.
- 검증 뒤 이번에 시작한 Ollama11434/Agent8001/Vite5173·임시 탭을 종료하고 listener 부재 확인. 모델/배치 자동 선택·다운로드/외부전송/넥슨0·두 승인 종료/재사용 금지. 지속 데모 서버 준비 완료가 아니다.
- 웹 공유 기준은 develop00623baa/API#79 및 agent-browser-e2e.md다. 새 웹 소스 변경 요청 없음. fixture 재생성/웹 추가 기능/CI·경로 보호는 별도 후속이며 상대 수신/검증을 대신 주장하지 않는다. master에는 기능 묶음 PR만 생성·병합/배포하지 않는다.

### INT-024 — W4-2 실제 모델 ASGI 결과와 재개 (2026-10-21)

- 통합 / 실제 실행2026-10-20·재개 기록2026-10-21. 사용자 승인 agent-api-e2e-20261020-001, 상태2회·추론3회 사용·잠금/원본 보존·추가 호출0. 실제 모델qwen3:8b/고정digest와 합성 배치·관측 시각은 synthetic-agent-api.md에 기록했다.
- 정상optimal11명·급여 누락/예산 충돌needs_clarification. 최초 검사기 급여 int/문자열 비교 실패 manifest 보존, 저장 응답만 계약 타입으로 검증해3/3 통과. 모델/제품 오류를 숨기기 위해 재시도하거나 결과를 고치지 않았다.
- 최신 develop d337535의 WEB-004/PR#78 반영 후 저장 실제3스트림/12이벤트가 TS파서 통과(Node24.19.0). 웹 코드/WEB-* 변경 없이 읽기 전용 대조. 선택 issue.span/coverage 제안 철회는 WEB-004를 확인했으며 백엔드 wire 추가 없음.
- 이번 PR은 HTTP 앱 구현과 실제 ASGI 검증까지다. 8001 상주 서버·브라우저 live 연결/신규 넥슨 수집0. VITE_FRS_API_BASE_URL=http://127.0.0.1:8001은 앱을 새 승인 범위로 실제 시작한 뒤 사용한다. 기존 mock을 live로 표시하지 않는다.
- 합성/실모델 제한·검사기 오류·해시·후속은 synthetic-agent-api.md. 이전 INT-023의 실제 실행 대기 상태는 이 결과가 대체한다. 병합 SHA·CI는 PR의 실제 결과를 따른다. 상대 확인/메시지 전송 주장 없음.

### INT-023 — W4-2 합성 Agent HTTP/SSE 앱 (2026-10-20)

- 통합 / 구현·합성 검증. 기준 develop0d9f9a3(#77), feat/synthetic-agent-api. #77은 head3d0147c/CI39391464235 성공 후0d9f9a3로 Squash 병합·메시지/트리 확인·완료 브랜치 로컬/원격 삭제.
- 구현: 별도 FRS.api.agent_app 앱, GET /health·POST /recommend/squad/stream. wire web-agent.v0, health agent-health.v1. origin2개/credentialsfalse·64KiB/ID/150초·단일실행/409·보수적 중단. 기존 core/graph/presentation/API8000/웹·WEB-* 보존.
- 요청별 worker/lease·후속 검사와 실제 Future 완료 기반 슬롯 해제, 캐시한 시작 메타데이터 기반 health/미검증·만료503, 명시 설정/합성 배치·시각 고정. 실제 시작 시 상태 조회2회가 있으므로 별도 실행 범위 확인 필요.
- 검증: 신규31개/전체pytest1,440개·Ruff·환경/pip 통과. ASGI2.4 끊김/기한 후409유지·후속 자료 차단·전송 전 만료/헤더 실패·모델 오류를 대역으로 검증. 실제 ASGI9스트림/36이벤트 TS파서 통과(Node24.19.0). 원문/큰수/단일terminal 유지.
- 웹 공유: 백엔드 wire 구현/합성 대조 기준은 이 PR 병합 SHA부터다. VITE_FRS_API_BASE_URL 후보는 http://127.0.0.1:8001이나 아직 상주 서버/모델 E2E를 실행하지 않아 연결 준비 완료가 아니다. 기존 mock을 실제 성공으로 위장하지 않는다. 보호 코드 수정 요청 없음·상대 수신/검증 주장 없음.
- 실제 Nexon/Ollama 요청0회. 정상/누락/충돌의 새 소량 실제 모델 E2E(최대2상태+3추론)는 별도 질문 대기이며 답변 전 실행하지 않는다. 상세 synthetic-agent-api.md.

### INT-022 — W4-1 웹 경계 순수 코드 (2026-10-20)

- 통합 / 구현·합성 검증. 기준 develop53446d6(#76), feat/agent-web-contract. 정책 #76은 headebe7fe3/CI39390911643 성공 후53446d6로 Squash 병합·메시지/트리 확인.
- DTO/원문·준비/정규화 투영·안전한 선택 오류·명시 버전/모델 출처·불변 bytes/최종 만료·64KiB 엄격 요청·요청별 SSE를 추가. 기존 core/graph/presentation·웹/WEB-* 변경 없음.
- 신규55개/전체pytest1,409개·Ruff·환경/pip 통과. TS parser 읽기 전용 대조는12final/42envelope·추가필드12거부 통과(번들Node24.19.0). 전체 웹 빌드/브라우저/실제 HTTP·모델 E2E 아님.
- 후속 사용자 확정: 8001 GET /health는 설정/합성 유효기간/시작 시 모델명·digest·검증 시각을 표시, 미검증·만료503·조회마다 모델 HTTP 없음. 실제 상태 확인/서버 실행은 별도 범위.
- 다음: 최신 head CI/develop 병합 후 HTTP 앱 수명주기/동시성·기한/연결 종료를 대역으로 구현. 실제주소 준비·웹 live 연결 완료 아님. 상세 agent-web-contract-implementation.md.

### INT-021 — 웹 경계 구현 재개·HTTP 정책 확정 (2026-10-20)

- 통합 / 상태: 사용자 결정·계약. 기준 develop b82e1c9(#74), docs/agent-http-runtime-policy.
- 사용자 다음 단계 요청으로 DTO/준비 사유·결과 투영/요청·SSE 직렬화·합성 검증부터 구현. 기존 core/graph/presentation·웹/WEB-*는 보존한다.
- 후속 확정: localhost:5173/127.0.0.1:5173 두 HTTP origin·credentials=false, ID ASCII 영숫자/-/_ 1~64자·64KiB, 전체 응답150초·진행 작업 종료까지 슬롯 보유. 새 실제 호출/서버 실행 승인은 아님.
- 실제 HTTP 앱/취소·readiness/웹 합성 계약 검증은 후속. 기존 v0 strict 필드를 기준으로 구현하며 실행하지 않은 진행/모델·단계를 사용으로 표시하지 않는다.
- 문서 내용/링크/diff 검토. 기능 테스트 재실행 없음(본 PR은 문서만). 모델/메타데이터·넥슨 요청0. 구현/검증 완료와 구분한다.

### INT-020 — 모델 채택·HTTP/SSE 경계 설계 (2026-10-20)

- 통합 / 상태: 사용자 결정·문서 설계. 기준 develop7c56bbc(#72), docs/agent-http-sse-contract. C4는 headc0cadee/CI39386783038 성공 후7c56bbc로 Squash 병합하고 최종 표기/트리 확인·완료 브랜치 로컬/원격 삭제.
- 사용자 확정: qwen3:8b를 첫 정형 입력 데모 추출로 채택. 다음은 HTTP/SSE 설계만이며 API 코드/새 모델 호출/웹 수정 제외.
- WEB-002의 strict v0/seq0/terminal/문자열 수치/상태 불변 조건을 실제 frontend 소스와 읽기 전용 대조했다. 내부 presentation 필드/모델 출처·준비 사유 투영·그래프 진행 hook 부재·기존 Mongo lifespan의 간극을 agent-http-sse-contract.md에 기록.
- 설계안: 기존 도메인 보존·HTTP DTO/실행 계층만 추가, 준비 명확화/선택 기술 오류 구분, 원문/ID/기한/정밀도 유지. 실제 API/v0 호환성 통과·서버 주소 준비 완료 아님. 그래프 완료 목록을 실시간 진행으로 위장하지 않음.
- 후속 사용자 확정: 별도 합성 앱/127.0.0.1:8001, 동시1/대기0/초과409/끊김 뒤 보수적 종료, seed123/members_per_family3/명시 배치·관측 시각 고정. 구현/서버/생성 실행 승인은 아님. origin/본문/총 기한 등은 미정. 웹은 live 연결/보호 코드 수정 대기, 상대 수신/동의/재검증을 주장하지 않음.
- 문서/링크/diff 검토, 기능 테스트 재실행 없음(문서만). 이전 전체pytest1,354개·Ruff·실제12회 결과는 #72 기준. 이번 새 Nexon/Ollama 요청0회·기존 잠금/기록 보존.

### INT-019 — C4 코드·실제 소량 비교 결과 (2026-10-20)

- 통합 / 상태: 구현·검증·실제 비교 완료. 기준 develop1f05b44(#71), feat/ollama-candidate-selection. INT-018/C4 계약을 구현.
- ollama_selection·공통 ollama_transport·candidate_comparison을 추가하고 직접 구간 추출기 공개 계약을 보존. 기존 normalizer/core/graph/presentation/API·웹/WEB-* 변경 없음.
- 신규37개/전체pytest1,354개·Ruff·환경/pip 통과. 실제 candidate-selection-20261020-001은 상태4회·추론12회 completed, 서버/연결/기한/재시도0.
- 2b 전체근거/의미5/6·8b6/6. 2b의 순서/쉼표 사례 강화ID 누락은 incomplete_coverage로 거부. 두 모델 각 미지원1개 준비 차단/미호출. 중앙 지연4.59/9.00초(로드 포함). 스키마는 모두6/6.
- 8b를 정형 데모 후보로 추천하되 모델 채택/HTTP·SSE 운영 정책은 사용자 확인 후 결정. 실제 비교에서 엔진/DB/수집/API/웹은 미실행. 웹 DTO 확정 아님·웹 담당 수정 요청 없음.
- 결과/계획/manifest/모델 digest·해시는 ollama-candidate-selection-results.md. 기존 실행/잠금 보존, 새12회 소진·재실행 금지. 최신 head CI/PR/병합은 실제 완료 후 기록.

### INT-017 — 후보 선택 C1~C3 코드

- 날짜/담당: 2026-10-20 KST / 통합. 상태: 구현·로컬 합성 검증 완료, 원격 CI 대기.
- 기준: develop 88a1b28(PR #69), feat/controlled-candidate-selection. C0는 head 47c9f3c/CI39383810778 성공 후 88a1b28로 Squash 병합, 최종 메시지/트리 확인 후 완료 브랜치 로컬·원격 삭제. 구현 브랜치에 계약을 통합.
- 변경: candidates/selection/wrapper의 순수 준비·불변 registry·엄격 ID·전체 출현 커버리지·SourceSpan 복원→기존 요청별 그래프. 준비 사유·선택 오류는 별도 결과로 종료하고 정상 입력만 기존 AgentRunResult에 위임.
- 검증: 신규 84개/전체 pytest 1,317개·Ruff·환경/pip 통과. 3클럽×3포메이션 실제 CP/독립 검증/표·순서120개·값/구간/ID/변조·상한/Unicode/중첩·미호출/만료/외부 tracing 비활성화를 확인. 실제 모델 호출 0회. 상세 controlled-candidate-selection.md.
- 호환: 기존 Ollama JSON 함수를 공통 모듈로 이동/재노출하고 회귀 확인. core/기존 graph/presentation/API/frontend·WEB-* 변경 없음. 새 wrapper는 HTTP/v0가 아니며 입력 사유 직렬화는 후속.
- 다음: 최신 head CI/병합/브랜치 정리 후 기능 묶음 develop→master PR 생성. 실제 Ollama ID 선택 C4는 모델/범위·새 호출 승인 필요. 두 직접 구간 모델은 여전히 채택 보류.



### INT-016 — 후보 선택 구현 계약 확정

- 날짜/담당: 2026-10-20 KST / 통합. 상태: 사용자 결정 반영·C0 계약.
- 기준: develop 2aef28b(PR #68), docs/candidate-implementation-contract. master PR #65가 다른 실행 주체에 의해 790907b로 병합됐음을 읽기 전용 확인. 파일 내용은 당시 develop과 같고 이 대화에서 master를 병합하지 않음.
- 사용자 결정: 정형 조건문 우선·순서 자유·누락/중복 보존, 미해결/부정/정정은 전체 명확화. 값 후보 128개/문맥 64개 상한·초과 전체 명확화, 잘못된 모델 ID/응답은 기술 오류·재시도/기본값 없음.
- 구현 범위: 후보 생성/불변 registry·엄격 ID 선택/원문 복원·새 입력 준비/선택 결과 wrapper→기존 SquadAgent. 정상 문맥은 한 값씩이므로 전체 ID 커버리지로 대조, 미해결 문맥을 모델 filler 판정으로 무시하지 않음.
- 경계: 실제 모델 호출/선정·새 수집·웹/기존 엔진/API·공개 정규화 의미 변경 없음. 상세 candidate-selection-implementation-contract.md. 코드/합성 검증은 후속 기능 PR에서 기록.



### INT-015 — 후보 구간·ID 선택 설계

- 날짜/담당: 2026-10-20 KST / 통합. 상태: 사용자 선택에 따른 설계 초안, 구현 아님.
- 기준: develop 22b7073(PR #67). 비교 기록은 head 12cda58/CI 39381098614 성공 후 22b7073으로 Squash 병합하고 최종 메시지/트리를 확인, 완료 브랜치 로컬·원격 삭제.
- 사용자 결정: 코드가 원문 후보 구간/ID를 생성하고 모델이 ID를 선택하는 어댑터의 설계부터. 기존 정규화/엔진 원문 검증 유지, 추가 모델 호출 없음.
- 설계: 값/문맥 registry·원문/집합 바인딩·출현별 ID·ID 존재/필드/중복 검증·문맥 커버리지·SourceSpan 복원 후 기존 정규화. 전 출현 자동 채택·인덱스 보정·fallback/재호출 없음.
- 미정/간극: 통제 문형/허용 표현·후보 상한(128/64는 제안)·새 명확화 사유/출력 형태. 현재 ExtractedConditions/ExtractionError 인터페이스만으로 입력 명확화를 전달하는 계약이 없으므로 새 준비 결과/래퍼를 후속 제안으로 구분. 실패를 missing이나 normalizer 결과로 위장하지 않음.
- 영향: 문서만. frontend/·WEB-*·엔진/Agent/API 코드/의존성 변경 없음. 원본 정규화/스냅샷/독립 검증/만료 유지. 상세 candidate-selection-adapter-design.md.
- 다음: 설계 검토 뒤 최초 구현의 문형·제한·오류 전달/범위를 사용자와 확정. 두 번째 비교 10회는 소진됐고 추가 실행 없음.



### INT-014 — 승인된 새 Ollama 비교 결과

- 날짜/담당: 2026-10-20 KST / 통합. 상태: 실제 소량 비교 완료·현재 방식 모델 채택 보류.
- 기준: develop 768e0a5(PR #66), docs/ollama-model-comparison. 코드/프롬프트/스키마/정답은 PR #64와 동일.
- 사용자 결정: 첫 실행/잠금 보존, 설치 서버 시작과 같은 조건의 새 비교 1회. 새 승인 디렉터리 approval-20261020-002·실행 ollama-extraction-20261020-002, 최대 10회·각 120초·순차·재시도/다운로드/외부 전송 없음.
- 실제 결과: 상태 확인 4회·추론 10회 완료, HTTP/연결/타임아웃/재시도 0. qwen3.5:2b/qwen3:8b 모두 스키마 5/5, 전체 구간/근거/의미 0/5. 지연 중앙값 9.39/11.81초(매 요청 로드 포함). 값 접두어·조사 포함/분절 오류도 있어 인덱스 보정만으로 성공을 만들지 않음.
- 모델/후속: 둘 다 현재 직접 구간 생성 방식에서 채택 보류. 코드 후보 구간/ID 선택 설계 또는 현 계약 프롬프트 개선 설계를 질문. 답변 전 새 계약/추가 호출 없음. 서버는 loopback/cloud 비활성화로 시작하고 종료하지 않음.
- 검증/보관: 실제 완료 manifest/계획/소스 해시와 집계를 비교 기록에 수록. 원래 중단 manifest와 새 content/상세 지표는 Git 제외 보존. 문서만 변경, 로컬 기능 테스트 재실행 없음. 원격 CI는 실제 완료 후 기록.
- 웹 공유: 원문 근거 실패는 검증 단계에서 거부, 실제 Agent API/SSE·웹 연결 없음. frontend/와 WEB-* 항목은 수정하지 않음. 1-D/1-F·실데이터/공식 보너스도 미완료.



### INT-013 — 기능 묶음 master PR과 후속 정리

- 날짜/담당: 2026-10-20 KST / 통합. 상태: PR 생성·정리/검증 기록.
- 사용자 기준: 기존 완료분 포함 로컬·원격 정리, 동작 가능한 기능 묶음 검증 후 develop→master PR 생성. 병합/배포는 별도 요청.
- 결과: 기능 묶음 PR #65 생성, master 미병합. master 밖 두 커밋은 과거 merge이며 파일 차이 없음; merge-tree로 충돌 없음/develop과 동일 tree 확인. master 대상 CI 자동 실행은 기존 설정에 없음을 PR에 명시.
- 추가 확인: 보류했던 docs/llm-candidate-comparison·feat/position-api는 각각 PR #53/#32 최종 squash 커밋을 가리키며 develop에 포함됐다. 현재 head/원격 부재/활성 작업 부재를 확인해 로컬 두 개 삭제. 완료 PR #62/#63/#64의 로컬·원격도 삭제. 현재 누계 로컬 58·원격 56, 사용 중/별도 담당은 보존.
- 계약/웹 영향: frontend와 WEB-* 수정 없음. 원격에 반영된 웹 모의 구현을 보존하고 실제 채팅 연결을 완료로 표시하지 않음. 새 LLM 실행/모델 선정은 사용자 판단 대기.
- 다음: 이 문서 PR 검증/병합 후 그 브랜치도 정리하고 PR #65 본문을 최신 SHA/검증으로 갱신. develop은 보존하고 master 병합하지 않음.



### INT-012 — R4 로컬 추출 어댑터·비교 사전 점검 중단

- 날짜/담당: 2026-10-20 KST / 통합. 상태: 코드/합성 검증 병합 완료, 실제 비교 대기.
- 기준: develop 7604585(PR #63 정책 병합), feat/local-condition-extraction. R3 PR #62는 최신 head 58e0484/CI 39378396213 통과 후 be2b720으로 Squash 병합. 정책 PR #63은 head 546f979/CI 39378812926 통과 후 7604585로 Squash 병합. 최종 메시지/트리 확인, 두 완료 작업 브랜치 로컬·원격 삭제(이전 53개와 별도).
- 변경: 고정 loopback Ollama 추출기, 여섯 필수 배열/엄격 JSON·원문 근거, 120초 전체 기한·재시도 없음. 같은 5문장/정답의 최대 10회 비교, pending 선기록/승인 잠금·지표 분리.
- 실제 결과: 첫 실행 ollama-extraction-20261020-001은 서버 연결 거부로 사전 점검 1회·추론 0회에서 종료. 기록을 보존했으며 재호출하지 않았다. 서버 시작/새 비교는 사용자 답변 대기. 모델 선정·실측 성공률 없음.
- 검증: 신규 합성 41개, 목킹한 Ollama→그래프→실제 CP-SAT/독립 검증/표 연결 포함. 전체 pytest 1,233개·Ruff·환경/pip 통과. head 2b67f93의 CI 39379359111 성공 후 PR #64를 aeb23a4로 Squash 병합하고 최종 표기/트리를 확인. 상세 ollama-extraction-comparison.md.
- 웹 공유: v0 모의 DTO를 확정/변경하거나 실제 API/SSE를 추가하지 않는다. 모델 메타데이터·서버 주소/CORS/동시성/취소 정책과 INT-001~005는 후속.



### INT-011 — 완료 브랜치와 기능 묶음 PR

- 날짜/담당: 2026-10-20 KST / 통합. 상태: 사용자 결정 반영·1차 정리 완료.
- 변경: 기존 완료분 포함 로컬·원격 삭제, 기능 묶음 검증 완료 시 develop→master PR 생성. master 병합·배포는 별도 요청.
- 검증: 현재 head와 develop에 도달한 병합 PR을 대조해 53개씩 정리. 보호 브랜치·원래 작업 디렉터리·별도 웹 담당 브랜치·확인 불충분 로컬 브랜치는 보존. 상세 git-workflow-policy.md.
- 웹 공유: 웹 PR #61이 다른 실행 주체에 의해 develop 35fa218에 일반 merge된 것을 관찰했다. 이 대화에서 병합한 것으로 보고하지 않으며 기존 이력을 재작성하지 않는다. WEB-* 기록을 보존해 백엔드 출력 PR에 최신 develop을 통합했다. 웹/API 실제 연결은 아직 미완료다.
- 다음 조치: 이후 각 작업 완료 때 같은 확인/정리. 현재 추출 비교 후 동작 가능한 내부 백엔드 기능 묶음의 master 대상 PR에 제한과 검증 범위를 명시.



### INT-010 — R3 코드 출력과 최종 만료

- 날짜/담당: 2026-10-20 KST / 통합.
- 상태: 구현 / 로컬 검증 완료, 원격 CI 대기.
- 기준: develop 4260a8d(PR #60), feat/validated-agent-presentation.
- 변경: 정규화/build/스냅샷 일치와 독립 검증 재확인 후 11명 표·정확한 문자열 BP/급여/ID·코드 템플릿·출력 직전 만료. 비성공/변조/비합성 자료는 성공 표 없음.
- 검증: 신규 16개·전체 pytest 1,192개·Ruff·환경/pip 통과, 최신 head CI는 완료 후 기록.
- 웹 공유: 내부 kind=code_presentation이다. web-agent.v0/HTTP 계약 확정은 아님. 모델 메타데이터는 미제공으로 기록하며 실제 API에서 모델 출처/요청 ID/전송 직전 기한을 추가 연결해야 함.
- 다음 조치: 검증/병합 후 승인된 R4 로컬 추출 비교. 웹 파일/실데이터/새 수집·실제 모델 서술 없음.

### INT-009 — R2 테스트 대역 그래프

- 날짜/담당: 2026-10-20 KST / 통합.
- 상태: 병합 완료.
- 기준: develop 2ce7ddb(PR #59), feat/injected-agent-graph, backend-agent-mvp 전용 작업 디렉터리.
- 변경: 추출기/자료/배치/시계 주입·extract/normalize/조건부 build, 단일 요청·재시도/이력 없음. LangGraph 1.2.16/LangSmith SDK 0.14.11 및 새 22패키지 고정, 기존 고정값 유지.
- 검증: 신규 23개·전체 pytest 1,176개·Ruff·환경/pip·고정 해석 통과. head f5d5159의 CI 39357471708에서 새 환경 설치/전체 검사 성공, PR #60을 4260a8d로 Squash 병합하고 최종 표기/트리 확인.
- 웹 공유: 내부 AgentRunResult만 추가. 표/HTTP final 이전의 최종 기한 확인은 R3 이후 필요하다. 웹 모의 v0/주소/취소 정책은 확정하지 않음.
- 다음 조치: 검증/병합 후 R3 코드 출력과 R4 승인된 로컬 모델 비교. 아직 실제 추론/외부 tracing/새 수집 없음.

### INT-008 — R1 합성 자료 공급기

- 날짜/담당: 2026-10-20 KST / 통합.
- 상태: 병합 완료.
- 기준: develop a4c98ad(PR #58), feat/synthetic-agent-material, backend-agent-mvp 전용 작업 디렉터리.
- 변경: 명시 run_id/seed/시각/생성량의 합성 리그, 원본 1강→현재 5강·5~11강 자료, 명시 배치 공급기. 가격/급여/자격 규칙은 독립 합성으로 기록.
- 검증: 신규 22개·전체 pytest 1,153개·Ruff·환경/pip 통과. head 46a6518의 CI 39356577729 성공 후 PR #59를 2ce7ddb로 Squash 병합하고 최종 표기/트리를 확인.
- 웹 공유: 내부 자료 주입 계층만 추가. 웹 파일/DTO/API/SSE 제안은 변경하지 않음. 실제 서버 경로·계약·접속 정보는 아직 대기.
- 다음 조치: 전체 검증/병합 후 R2 테스트 대역 그래프. 실데이터/학습/모델 호출/새 수집 없음.

### INT-007 — R0 강화 계산기

- 날짜/담당: 2026-10-20 KST / 통합.
- 상태: 병합 완료.
- 기준: develop 101e2e8(PR #57). backend-agent-mvp 작업 디렉터리, feat/reinforcement-derivation.
- 변경: FRS/core/reinforcement.py 순수 계산, 34개 키 공유 상수, 기존 학습 입력의 상수 재사용. 누적값·현재 40피처 해시·추천 5~11 범위 유지.
- 검증: 신규 합성 34개·전체 pytest 1,131개·Ruff·환경/pip 검사 통과. 최신 head 1a451b6의 CI 39355861243 성공, PR #58을 a4c98ad로 Squash 병합하고 최종 표기/트리 확인. 가격·급여·메타데이터·원본 상태/기한·웹 파일 변경 없음.
- 계약 영향: web-agent.v0/API는 바꾸지 않음. source payload 해시는 HTML/manifest 검증을 뜻하지 않으며 결과는 독립 파생 자료로만 반환.
- 다음 조치: 전체 검증/PR 병합 후 R1 합성 MaterialProvider에서 현재 5강 계약에 연결.

### INT-006 — 백엔드 개발 재개

- 날짜/담당: 2026-10-20 KST / 통합.
- 상태: 사용자 요청 반영 / 구현 준비.
- 기준: develop 47c43f1(PR #56). backend-agent-mvp 전용 작업 디렉터리, docs/backend-resume-contract에서 범위를 먼저 기록한다.
- 변경 전 → 후: 웹 인계 문서 단계 → 웹 담당 범위를 제외한 백엔드 기능 개발 재개. R0 강화 계산 후 R1 합성 자료/R2 테스트 대역 그래프/R3 백엔드 출력/R4 모델 비교 순서.
- 계약 영향: web-agent.v0는 여전히 모의 제안이다. 이 재개로 실제 채팅 API/접속 정보를 확정하거나 웹 파일을 변경하지 않는다.
- 사용자 결정: qwen3.5:2b/qwen3:8b에 합성 5개씩 총 최대 10회 로컬 비교, 순차·120초·재시도/다운로드/외부 전송 없음. 모델 선택/설정·동시성 등 남은 정책을 이미 확정한 것으로 간주하지 않는다.
- 검증/차단: 구현/실행은 후속 증분에 실제 결과를 남긴다. frontend/와 WEB-* 항목을 수정하지 않는다. 새 넥슨 수집·실데이터 변환·학습 없음.
- 다음 조치: docs/backend-resume-plan.md를 기준으로 별도 기능 PR에서 R0 구현/전체 회귀 검증.

### INT-000 — 초기 분담 설계

- 날짜/담당: 2026-10-20 KST / 통합.
- 구분/상태: 사용자 결정 반영 / 문서화.
- 근거: 사용자가 웹을 다른 에이전트에 맡길 설계도와 공용 기록, 기존 엔진/Agent 변경 방지책을 요청했다. 후속 답변으로 Vue 3/Vite/TypeScript·웹 클라이언트 한정·백엔드 통합 전담을 확정했다.
- 변경 전 → 후: 공동 작업 경계 없음 → frontend/ 소유권·읽기 전용 보호 경로·변경 요청 절차·v0 모의 계약·통합 병합 게이트.
- 영향: 웹 모의 개발을 백엔드와 분리한다. 실제 연결과 공통 설정 변경은 통합 담당이 처리한다. 제품/데이터/비용 정책을 확대하지 않는다.
- 검증/제한: 현재 코드의 세 API·정규화/엔진 상태·정수 범위·Python CI를 읽어 경계를 확인했다. 웹 앱/자동 경로 차단/웹 CI/채팅 API를 구현한 것은 아니다.
- 관련 문서: web-agent-blueprint.md, frontend/AGENTS.md, 루트 AGENTS.md, 로드맵, 기준 설계도 §5/§11.
- PR/기준 SHA: 이 문서 PR은 최종 원격 상태에서 확인. 시작 기준 ec6ff24.
- 다음 담당/차단: 웹 배정 시 WEB-*에 사용 기준 SHA·작업 브랜치·도구 버전 계획을 기록. 실제 연결은 INT-001/002/005 반영 대기.

### WEB-001 — 웹 도구 버전 고정과 모의 클라이언트 구현

- 날짜/시간(KST), 담당/역할, 고유 ID: 2026-10-20 / 웹 / WEB-001.
- 구분: 진행. 상태: 구현·로컬 검증. 병합 아님.
- 사용한 develop SHA·작업 브랜치·작업 디렉터리 식별: 작업 시작 develop `47c43f1`(PR #56) → PR 전 `2ce7ddb`(PR #59)로 재기준. 그 사이 INT-006~008을 읽었으며 web-agent.v0·웹 경계 변경 없음을 확인 / `feat/squad-web-ui` / 통합 담당과 다른 새 클론.
- 적용한 설계도/계약 버전·의존 기록 ID: web-agent-blueprint.md, frontend/AGENTS.md, `web-agent.v0` 모의 제안. 의존 INT-001~005.
- 변경 전 → 후: 프론트엔드 앱 없음 → frontend/에 Vue 3 + Vite + TypeScript 클라이언트, 모의 transport, 동작 테스트, README.
- 고정 버전: Node 22.x(`>=22.12.0 <23`, `.nvmrc`, `engine-strict`), npm 10.9.x 이상, vue 3.5.44, vite 8.3.4, @vitejs/plugin-vue 6.0.10, typescript 5.9.3, vue-tsc 3.3.13, vitest 5.0.5, jsdom 27.4.0, @vue/test-utils 2.4.6, @types/node 22.20.5. 근거: Vite 8·Vitest 5의 Node 요구, TypeScript 7은 vue-tsc 호환 미확인, jsdom 30·test-utils 2.5는 Node `^22.22.2` 이상을 요구해 검증 환경 v22.22.0에서 설치 거부. lock 파일은 frontend/package-lock.json.
- 웹 검증 명령: `cd frontend && npm ci && npm run typecheck && npm test && npm run build`. CI 통합(INT-004)은 통합 담당 결정 대기이며 스크립트 이름은 위와 같이 제안한다.
- 영향 경로·호환성: frontend/ 하위와 이 기록의 WEB-* 항목만 변경. FRS/·tests/·scripts/·Python 의존성·Docker·CI·설계 문서 변경 없음. 실제 연결은 `VITE_FRS_API_BASE_URL`이 있을 때만 선택 가능하며 기본값 없음. 개발 서버 proxy·CORS 미설정.
- 사용자 결정 필요 여부: 없음(확정 스택·분담 범위 안).
- 검증 결과(재기준 2ce7ddb, 로컬): `npm ci` 후 typecheck 통과, Vitest 5개 파일 62개 통과, build 통과. headless Chromium으로 1360px·390px 화면의 정상·되묻기·큰 금액·프로토콜 오류 상태 확인. 루트 Python 필수 검사 pytest 1,153개 통과·ruff check·ruff format --check 통과(Python 3.11.17, 고정값 3.11.15와 패치 버전 차이). 미실행: 웹 CI(미구축), 실제 합성 Agent와의 대조(백엔드 경로 부재), 원격 CI는 PR에서 확인.
- 다음 담당/해야 할 일: 통합 담당이 WEB-002의 DTO 피드백을 검토하고 INT-001에서 확정 버전을 기록. 확정·병합 SHA가 공유되면 웹이 live transport를 조정한다.

### WEB-002 — v0 DTO 피드백 (웹 구현에서 정한 해석)

- 날짜/담당/ID: 2026-10-20 / 웹 / WEB-002. 구분: 설계 차이·변경 요청. 상태: 제안. 통합 담당의 확인 전이며 확정 계약이 아니다.
- 기준: develop `2ce7ddb`의 web-agent-blueprint.md §4~§5(47c43f1 이후 변경 없음). 아래는 설계도가 열어 둔 부분을 모의 구현에서 정한 해석이다. 백엔드 어댑터가 다르게 정하면 웹을 맞춘다.
  1. envelope와 payload는 정의된 필드만 허용한다(알 수 없는 필드는 프로토콜 오류). 첫 이벤트는 seq 0의 started다.
  2. `versions`는 문자열 값의 객체이며 `web_contract: "web-agent.v0"`를 필수로 본다. 모델 미사용은 `model: "unused"`로 표기했다.
  3. `answer`는 `narration_source`가 none이면 null, 그 외에는 문자열이다.
  4. 성공(optimal/feasible)은 `conditions`·`squad`·`snapshot.valid_until`이 필수이고 `issues`가 비어 있어야 한다. needs_clarification은 `conditions: null`, issues 1개 이상이다. 그 밖의 상태는 `squad: null`이다.
  5. 행의 `spid`·`person_id`는 10진 정수 문자열, `price`는 최대 34자리, `salary`는 2^63-1 이하 문자열이다. `total_price`·`total_salary`도 문자열이다. `raw_ovr`·`total_ovr`는 정수, `performance`는 0.5 단위 number다.
  6. 클라이언트는 행 11개·포메이션 자리 집합·인물/카드 중복·허용 강화·합계·상한을 대조하고 불일치 시 성공 표시를 거부한다. 보정은 하지 않는다. 포메이션 자리표는 FRS/schema/scope.py의 FORMATION_SLOTS를 읽기 전용으로 옮겨 적었다.
  7. 시각은 `Z` 또는 `±HH:MM`이 있는 ISO 8601만 받는다.
  8. 종료 이벤트를 받은 읽기 단위의 뒤 이벤트는 프로토콜 오류로 처리하고, 종료 이벤트 뒤에는 서버의 연결 종료를 기다리지 않고 읽기를 멈춘다.
- 영향: 백엔드 웹 어댑터(통합 담당 소유)의 직렬화 규칙. 기존 Python 도메인 모델 변경은 요청하지 않는다.
- 다음 담당: 통합 담당. 받아들이거나 다르게 정한 항목을 INT-001에 적용 버전과 함께 기록.

### WEB-003 — 정형 조건문 입력 안내와 v0 선택 필드 제안

- 날짜/담당/ID: 2026-10-20 / 웹 / WEB-003. 구분: 진행 + 설계 차이·변경 요청. 상태: 구현·로컬 검증(웹), 계약 부분은 제안. 통합 담당의 확인 전이다.
- 기준: develop `7c56bbc`(#72). INT-015~019, FRS/agent/candidates.py·candidate_graph.py·presentation.py를 읽기 전용으로 확인했다. 백엔드 파일은 바꾸지 않았다.
- 설계 차이 1 — 입력 예시: 웹 예시(`리버풀 4-2-3-1, 예산 6조 BP, 급여 210 이하, 5~8강 카드로 스쿼드 짜줘` 등 3개)는 7c56bbc의 `prepare_candidates`에서 모두 needs_clarification이 된다. 최소 재현 `리버풀 4-2-3-1, 예산 6조 BP, 급여 210 이하, 5~8강으로 구성해줘` → `unrecognized_context`("리버풀 4-2-3-1", 클럽·포메이션 한 절), `missing_coefficient`("6조 BP"), `invalid_salary`("210 이하").
- 변경 전 → 후(웹): 예시와 입력 안내를 쉼표로 나눈 정형 조건문(`예산 X, 급여 N, 클럽, 포메이션, N강|N~M강|N강,M강` + 선택 접미사 `(으)로 구성해줘`)으로 바꿨다. 새 예시 3개(`frontend/src/contracts/requestFormat.ts`)는 같은 기준에서 `prepare_candidates` issues 0개, 선택 ID를 모두 고른 뒤 `normalize_conditions` 통과를 확인했다. 브라우저는 여전히 원문을 검사·수정하지 않는다.
- 설계 차이 2 — 결과 필드: CodePresentation·후보 준비 결과에는 v0 final에 없는 `coverage`, 오류 사유 코드, 확인 시각, 문제 절의 원문 구간이 있다. 웹 v0 파서는 모르는 필드를 프로토콜 오류로 처리하므로 그대로 보내면 표시가 거부된다.
- 변경 요청(제안) — v0 final에 선택 필드 추가. 모두 생략 또는 null 허용, 그 밖의 모르는 필드는 계속 거부:
  1. `code`: `^[a-z][a-z0-9_:.-]{0,63}$`. 예: provider_error의 `selection_provider_error`·`invalid_selection`.
  2. `coverage`: `[{slot, count}]`. count는 0 이상 정수, slot 중복 금지, conditions가 있으면 FORMATION_SLOTS 순서와 같아야 한다.
  3. `evaluated_at`: 오프셋이 있는 ISO 8601.
  4. `issues[].span`: `{start, end, text}`. 0 ≤ start < end ≤ 4,000(Python 코드포인트, end 미포함), text는 비어 있지 않다. 웹은 text만 표시하고 위치로 원문을 자르지 않는다.
- 어댑터 매핑 요청(통합 담당 소유, 제안):
  1. `input_issues` → `needs_clarification`. PreparationIssue는 `{field, kind, code, message, occurrence, span}`으로 보낸다. 웹 모의 값은 field `original`이면 kind `invalid`, 값 파싱 실패도 `invalid`, occurrence 0이다. `context_id`는 보내지 않는다.
  2. `provider_error` → `llm_unavailable`(선택 호출 실패) 또는 `internal_error`(선택 결과 검증 실패)이며, `code`에 RunResult.error를 담는다.
  3. `agent_result` → CodePresentation의 status·conditions·squad·snapshot·coverage·evaluated_at을 그대로 옮긴다. kind·presentation_version·model_metadata는 `versions` 문자열 값으로 접어 넣거나 보내지 않는다. 템플릿 설명은 `narration_source: "template"`이다.
- 웹 표시: 되묻기 사유 옆에 원문 구간, infeasible·unknown에 자리별 후보 수(0인 자리 강조), 오류에 사유 코드, 출처 영역에 확인 시각을 보인다. 성공 결과에는 coverage를 따로 보이지 않는다. 모의 시나리오 `input_format`을 추가했고, infeasible·unknown·optimal·feasible에 coverage·evaluated_at, internal_error·llm_unavailable에 code를 넣었다.
- 영향 경로·호환성: frontend/ 하위와 이 WEB-003 항목만 변경했다. 새 필드가 없는 기존 v0 payload도 그대로 통과한다. FRS/·tests/·scripts/·CI·Docker·설계 문서 변경 없음.
- 사용자 결정 필요 여부: 없음(웹 범위). 계약 반영 여부는 통합 담당이 INT-001에서 정한다.
- 검증(로컬, 7c56bbc 기준): `npm run typecheck` 통과, Vitest 5개 파일 74개 통과(신규 12), `npm run build` 통과. headless Chromium으로 1360px·390px 화면의 입력 안내·input_format·infeasible·llm_unavailable·optimal 상태를 확인했고, 가로 넘침은 없었다. 루트 pytest 1,354개 통과, `ruff check`·`ruff format --check` 통과(Python 3.11.17). 미실행: 실제 Agent API 연결(경로 없음), 웹 CI(INT-004 대기).
- 다음 담당: 통합 담당. 선택 필드 4개와 어댑터 매핑을 INT-001에 수락/수정해 기록한다. 다르게 정하면 웹이 맞춘다.
- 후속(2026-10-20): 계약 제안 부분(선택 필드 4개·어댑터 매핑)은 INT-020 §4에 맞춰 WEB-004에서 철회했다. 입력 안내 변경은 유지한다.

### WEB-004 — 백엔드 W4-1 투영에 웹 맞춤·WEB-003 계약 제안 철회

- 날짜/담당/ID: 2026-10-20 / 웹 / WEB-004. 구분: 진행 + 철회. 상태: 구현·로컬 검증. live 연결 완료는 아니다.
- 기준: develop `4ff544d`(#75 병합). INT-020~022, agent-http-sse-contract.md §2~§8, agent-web-contract-implementation.md, FRS/api/agent_{contract,projection,events}.py를 읽기 전용으로 확인했다.
- 철회: INT-020 §4는 v0 final에 내부 kind·code·coverage·evaluated_at을 추가하지 않고, 준비 사유의 span·context_id를 내부에 둔다. 새 필드는 계약 버전과 양쪽 검사를 함께 바꾸는 방식이다. #75의 웹 파서는 그중 4개를 선택 필드로 받아서, INT-022의 "추가 필드 거부" 대조와 어긋났다. 그래서 파서를 #72 시점의 엄격한 v0로 되돌리고 관련 표시도 걷어냈다. 문제 절 구간 표시는 필요하면 다음 계약 버전 제안으로 다시 올린다.
- 변경 전 → 후(웹, frontend/만):
  1. 모의 응답을 실제 투영 모양으로 바꿨다: 준비 사유 `prepare.*`·occurrence null·field `original`, 정규화 누락 `missing_condition`, 템플릿 안내 문장, versions 13개 키, 진행 단계 `agent`·`render`만.
  2. 선택 실패와 기한 초과는 final이 아니라 `error` 이벤트(`selection_provider_error`·`request_deadline`)로 재생한다. v0의 llm_unavailable은 예약 상태로 화면 처리만 유지하고, 모의 목록에서는 뺐다.
  3. 공개 error 코드별 제목을 붙이고, 안내 문장은 서버 message를 그대로 쓴다.
  4. 스트림 전 HTTP 409·413·422·503을 상태 코드로만 구분해 안내한다. 본문은 해석하지 않고 다시 보내지 않는다.
  5. 개발 서버를 5173 포트로 고정했다(`strictPort`, INT-021 origin 두 개와 일치). `.env.example`에 8001 설계값을 적었다. 단, HTTP 앱은 아직 구현 전이다.
- 백엔드 대조: `frontend/tests/fixtures/generate_backend_fixture.py`가 백엔드 순수 투영/SSE 코드와 기존 테스트 대역(selection·OBSERVED·합성 config)을 읽기 전용으로 호출한다. 이것으로 `backend-w4-1.json`(성공 3·준비 사유 2·정규화 사유 1·error 2, 결정적)을 만들었다. 웹 테스트는 이 SSE 바이트를 1·7바이트 청크로 세션에 통과시키고, 파싱 결과가 payload와 같은지, 화면의 표·합계·만료 처리가 맞는지 확인한다. 모델·네트워크·DB·서버 호출은 없다.
- 영향 경로·호환성: frontend/ 하위와 WEB-003·WEB-004 항목만 바꿨다. FRS/·tests/·scripts/·CI·Docker·설계 문서 변경 없음. 서버가 v0를 그대로 보내면 웹 쪽 추가 조정은 필요 없다.
- 사용자 결정 필요 여부: 없음.
- 검증(로컬, 4ff544d 기준): typecheck 통과, Vitest 6개 파일 87개 통과, build 통과. headless Chromium으로 1360px·390px 화면을 확인했다(정형 조건문 아닌 요청·조건 추출 모델 오류·최적 결과, 그리고 409를 돌려주는 로컬 대역 서버로 실제 연결 모드). 가로 넘침은 없었다. 루트 pytest 1,409개 통과, `ruff check`·`ruff format --check` 통과(Python 3.11.17). 미실행: 실제 8001 HTTP 앱 연결(미구현), 웹 CI(INT-004 대기).
- 다음 담당: 통합 담당. 8001 HTTP 앱이 병합되면 기준 SHA를 INT-*에 남겨 주면 된다. 웹은 그 SHA로 fixture를 다시 만들고 live 연결을 확인한다.

### WEB-005 — 서버 상태 표시·HTTP 거절 안내·8001 앱 기준 대조

- 날짜/담당/ID: 2026-10-21 / 웹 / WEB-005. 구분: 진행. 상태: 구현·로컬 검증. 상주 서버 운영이나 새 실제 모델 호출은 아니다.
- 기준: develop `c0c47be`(PR #79 `00623ba` + 문서). INT-023~025, synthetic-agent-api.md, agent-browser-e2e.md, FRS/api/agent_app.py·agent_runtime.py를 읽기 전용으로 확인했다. INT-025의 "새 웹 소스 변경 요청 없음"에 따라 계약(web-agent.v0)은 바꾸지 않고, 후속으로 열어 둔 fixture 재생성과 웹 기능만 다룬다.
- 변경(frontend/만):
  1. 서버 상태: 실제 연결을 고르면 `GET {base}/health`(agent-health.v1)를 한 번 읽는다. ready면 모델·digest 앞자리·시작 시 확인 시각·자료 기준·유효 기한을 보인다. unavailable(data_expired·model_unverified·not_configured)이나 연결 실패면 사유를 보이고 보내기를 막는다.
     - 다시 조회하는 경우는 세 가지뿐이다: "다시 확인" 버튼, 연결 방식 재선택, 실제 요청 종료 직후. 주기 조회·재시도는 없다.
     - 브라우저 시계로 기한이 지났으면 안내만 한다. busy는 조회 시점 정보로 안내만 하고 막지 않는다.
  2. health 검사(`contracts/agentHealth.ts`): 정확한 필드, 스키마 버전, 사유 집합을 검사한다. HTTP 200⇔ready·503⇔unavailable도 대조한다. ready에는 모델 확인 기록과 자료 정보가 필수다.
  3. 스트림 전 거절 안내 추가: 403 origin_not_allowed, 408, 415. 서버 연결 실패 문구에는 8001 실행 여부와 5173 개발 서버로 열었는지를 안내한다.
  4. 대조 fixture를 `backend-agent-app.json`으로 교체했다. 실제 `create_agent_app()`을 TestClient와 테스트 대역 selector로 호출해 기록했으며, 출력은 실행할 때마다 같다.
     - SSE 7건: 성공 3, 준비 사유 2, 정규화 사유 1, 모델 오류 1
     - 스트림 전 거절 5건: 403, 415, 413, 422, 503
     - /health 4건
     - 409(busy)는 TestClient로 동시 요청을 재현하지 않아 웹 단위 테스트로만 확인한다.
- 영향 경로·호환성: frontend/ 하위와 이 WEB-005 항목만 바꿨다. 백엔드 wire·FRS/·tests/·scripts/·CI·Docker·설계 문서 변경 없음. 기존 모의 모드 동작은 같다.
- 사용자 결정 필요 여부: 없음.
- 검증(로컬, c0c47be 기준):
  - typecheck 통과, Vitest 6개 파일 108개 통과, build 통과.
  - 루트 pytest 1,440개·`ruff check`·`ruff format --check` 통과(Python 3.11.17).
  - 실제 8001 앱 코드를 테스트 대역 selector와 현재 시각 합성 자료로 127.0.0.1:8001에 임시로 띄웠다. Vite 5173 실제 연결 모드의 headless Chromium에서 다음을 확인했다.
    - 정상: optimal 11명, 진행 `agent`·`render`
    - 급여 누락·형식 오류: 되묻기
    - 서버 상태: 준비됨, 모델 미확인(보내기 막힘), 서버 꺼짐(연결 실패 안내·보내기 막힘)
  - Ollama·실제 모델·외부 요청은 0회이고, 띄운 임시 서버는 확인 뒤 종료했다.
  - 미실행: 실제 qwen3:8b 연결(통합 담당 승인 범위), 웹 CI(INT-004).
- 다음 담당: 통합 담당. 필요하면 상주 데모 실행 시 이 상태 표시를 같이 확인한다. 백엔드 wire나 health가 바뀌면 생성기로 fixture를 다시 만든다.

### INT-018 — C4 실제 ID 선택 어댑터·새 비교 승인 (2026-10-20)

- 담당: 통합 / 상태: 사용자 승인·계약. 기준 develop e234567 (#70), docs/candidate-ollama-contract.
- 의존: INT-017·candidate-ollama-comparison-contract.md. 기존 모델 제외 중 실제 어댑터/새 비교만 확대한다.
- 두 모델×7사례·준비 차단 미호출·최대12추론, 로컬 순차/각120초/재시도·다운로드·외부 전송 없음/서버·연결 오류 즉시 중단. 기존 기록 보존.
- 실제 비교는 정규화까지만, 엔진/DB/수집/API/웹 제외. 웹 DTO 변경 없음·웹 담당 조치 없음. 모델 운영 채택은 후속.
- 계약 PR은 문서/링크/diff 검토, 코드 테스트 미실행(문서만). 실제 구현/결과는 후속 기능 PR에 기록한다.

### 기록 양식 — 실제 작업 시 새 WEB-* 또는 INT-* 항목으로 복사

- 날짜/시간(KST), 담당/역할, 고유 ID:
- 구분: 진행 / 설계 차이 / 변경 요청 / 질문 / 통합 결과.
- 상태: 제안 / 확인 / 구현 / 검증 / 병합 / 차단 / 보류 / 철회.
- 사용한 develop SHA·작업 브랜치·작업 디렉터리 식별:
- 적용한 설계도/계약 버전·의존 기록 ID:
- 변경 전 → 후·이유·최소 합성 재현:
- 영향 경로·호환성·상대에게 필요한 작업:
- 사용자 결정 필요 여부·결정 근거/답변:
- 검증 명령·실제 결과·미실행 항목:
- PR URL·검증 head·병합 SHA(실제 확인 후):
- 다음 담당/해야 할 일·현재 차단:

## 인계/병합 체크

1. 양쪽은 각자 최신 develop과 이 기록을 읽고 자기 범위/의존 버전을 확인한다. 공유 파일은 실시간 채널이 아니므로 기준 SHA를 남긴다.
2. 웹은 자기 파일과 WEB-* 기록만 PR에 포함한다. 보호 파일 변경 요청은 별도 INT-* 응답과 통합 담당의 백엔드 PR로 해결한다.
3. 통합 담당만 PR 병합을 진행한다. 최신 head·필수 CI·전체 경로/내용·계약/설계 기록을 확인하고 Squash 제목/본문에 공동저자/생성 도구 표기가 없도록 한다.
4. 다른 PR 병합으로 develop이 바뀌면 충돌/호환 여부를 다시 확인한다. 현재 head의 검사 실패/미완료를 과거 성공으로 대신하지 않는다.
5. mock 완료 / 실제 합성 Agent 연결 완료 / 실데이터 완료를 각각 구분한다. 모바일 알림/상대 수신·배포·외부 실행을 확인 없이 완료로 기록하지 않는다.
