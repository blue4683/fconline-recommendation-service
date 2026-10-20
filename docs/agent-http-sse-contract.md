# Agent HTTP/SSE 경계 설계

2026-10-20 / 데모 W4·Stage4-B/4-C. 사용자 확정은 **첫 정형 입력 데모 추출 모델 qwen3:8b 채택**, **HTTP/SSE 계약 설계부터 진행**이다. 이번 작업은 문서이며 API 구현·추가 모델/메타데이터 호출·웹 변경·DB·수집·학습은 없다. 아래 wire 설계는 현재 웹 모의 클라이언트와의 연결안이며 실제 API의 존재/통합 검증 완료를 뜻하지 않는다. 후속 답변으로 별도 합성 앱/8001·동시1/대기0/보수적 종료·명시 합성 배치/시각 고정도 확정했다. 나머지 운영 선택은 §7에서 구분한다.

## 1. 읽은 기준과 연결 장애

기준 develop `7c56bbc`(C4 PR#72). [웹 인계 설계](web-agent-blueprint.md), [공용 WEB-002](agent-collaboration.md), `frontend/src/contracts/webAgentV0.ts`·`services/{liveTransport,agentSession}.ts`, `FRS/agent/{candidate_graph,presentation,ollama_selection}.py`, 기존 `FRS/api/{app,settings}.py`를 읽기 전용 대조했다. 상대가 읽었거나 동의했다고 기록하지 않는다.

| 현재 사실 | HTTP 경계에서 필요한 처리 |
|---|---|
| Agent HTTP/SSE 경로 없음. 기존 세 API는 카드/포지션 조회용 | 별도 Agent 라우트·의존성 주입/시작 설정이 필요 |
| 기존 API lifespan은 Mongo 연결과 선택적 XGBoost 번들을 준비 | 합성 Agent를 띄울 때 DB/학습 모델에 불필요하게 의존하지 않도록 별도 합성 앱의 독립 lifespan 구현 필요 |
| CandidateRunResult는 준비 문제/provider 오류/기존 Agent 결과의 wrapper | 세 결과를 혼동하지 않고 웹 DTO/SSE로 분기 |
| CodePresentation은 HTTP 계약이 아니며 kind/coverage/evaluated_at 등 추가 필드 존재 | 엄격한 허용 필드만 새 웹 DTO에 복사·검사. 내부 객체를 통째로 전송하지 않음 |
| 준비 문제에는 span/context_id가 있으나 웹 ConditionIssue 형태와 다름 | 웹 직렬화 계층에서 별도 사유 투영. 기존 normalizer 결과로 위장하지 않음 |
| 모델 정보는 기존 presentation에서 not_provided | HTTP 실행 문맥이 실제 호출 여부/모델명·digest·selector 버전을 별도로 고정 |
| 현재 그래프는 단일 호출 반환 후 stages를 제공 | 그래프 안의 실시간 단계 이벤트가 있는 것처럼 재생하지 않음 |

기존 엔진·normalizer·CandidateSquadAgent·presentation·스냅샷 검증을 화면에 맞추어 바꾸지 않는다. 웹 담당은 계속 화면/클라이언트만 수정하며 보호 파일 변경은 통합 담당의 별도 PR이다.

## 2. 요청·응답 경계안

기존 모의 `web-agent.v0` wire를 유지하는 안을 채택할 수 있도록 구체화한다. WEB-002의 strict 필드·started/seq0·문자열 versions·정확한 수치·상태별 불변 조건·ISO 시각·종료 뒤 추가 이벤트 거부를 반영한다. 버전 이름 유지 여부는 백엔드/클라이언트 합성 계약 대조를 마친 후 INT-001에 확정한다. 조용한 호환성 변경은 없다.

- 경로안: `POST /recommend/squad/stream`, JSON의 정확한 세 필드 `{contract_version, request_id, message}`. query에 원문을 넣지 않는다.
- message는 치환/trim/요약 없이 Python 코드포인트4,000자 이하 원문. 잘못된 타입·공백뿐·길이 초과·유효하지 않은 Unicode는 모델 호출 전 거부한다.
- request_id는 브라우저가 만든 요청 식별자로 그대로 검증/반환한다. 식별자만으로 중복 억제·동일 결과 재사용·서버 취소를 보장하지 않는다. 제안 문법은 ASCII 영숫자/`-`/`_` 1~64자이며 현재 UUID/hex 생성 방식과 호환된다. 아직 미확정 운영값이다.
- 브라우저는 배치/모델/시계·solver 설정/합성 데이터·검증 통과 플래그를 보내지 않는다. 서버가 명시 설정을 고정한다. 최신 모델/배치 자동 선택은 없다.
- 요청 구조/버전/길이 오류는 스트림을 열기 전에 HTTP422의 안전한 JSON 오류로 종료하는 안이다. 미준비 서비스는503, 동시 실행 초과는 사용자 확정한409로 스트림 전 거부한다. 웹 현재 클라이언트는 비2xx를 transport_error로 표시하며 세부 JSON을 화면에 쓰는 기능은 없다.
- 스트림 시작 뒤에는 HTTP 상태를 바꾸지 않고 terminal 이벤트로 종료한다. 응답은 UTF-8 `text/event-stream`, `Cache-Control: no-store`를 제안한다. 자동 재연결/재전송·리다이렉트·토큰별 응답은 없다.

본문 바이트 상한·서버 총 요청 기한·origin/포트·중복 ID 처리·readiness HTTP 형식은 구현 전에 결정/검증한다. 코드포인트 상한만으로 HTTP 바이트 수를 동일시하지 않는다. 브라우저가 HTTP422를 조건 명확화 화면으로 표시한다고 가정하지 않는다.

## 3. SSE와 실행 순서

공통 envelope는 `{contract_version, request_id, seq, type, payload}`의 정확한 필드다. event 이름과 type은 같고 seq는 요청마다0부터 1씩 증가한다. SSE data는 JSON이며 이벤트마다 빈 줄로 종료한다. heartbeat가 필요하면 주석만 쓰고 이벤트 번호를 소비하지 않는다.

| 이벤트 | payload | 의미 |
|---|---|---|
| started | `{data_mode:"synthetic"}` | 구조/서비스/실행 슬롯 확인 후 수락. 추천 성공 아님 |
| progress | `{stage,message}` | 실제 시작한 작업만 안내 |
| final | §4의 정확한 FinalResult | 도메인 결과·명확화·검증된 표/템플릿 |
| error | `{code,message}` | 안전한 기술 오류, 표 없음 |

연결이 정상인 수락 요청은 started 한 번과 final/error 중 정확히 하나를 보낸다. 연결이 끊긴 경우 terminal 전달을 보장하지 않는다. 마지막 이벤트를 전송한 뒤 다른 이벤트·JSON·진단 텍스트를 쓰지 않는다. 웹은 terminal과 같은 청크의 뒤 이벤트까지 검사하고 읽기를 중단하므로 서버의 늦은 연결 종료에 의존하지 않는다. EOF만으로는 성공이나 해 없음을 뜻하지 않는다.

첫 구현은 `progress(stage="agent")`로 요청별 후보 Agent 실행 시작, 실제 반환 뒤 `progress(stage="render")`로 웹 변환/출력 검증 시작만 알리는 안이다. prepare/select/normalize/build의 내부 진행을 완료 목록으로부터 거꾸로 실시간처럼 전송하지 않는다. 현재 템플릿만 사용하므로 narrate/check LLM 단계가 실행됐다고 표시하지 않는다. 세부 진행 hook이 필요하면 별도 설계/PR로 범위를 확대한다.

정상 경로: 요청·실행 슬롯 확인 → started → agent 시작 → 요청별 CandidateSquadAgent → render 시작 → present_agent_result/DTO 검사 → **직렬화 직전 원래 스냅샷의 만료 재확인** → final. 기한이 지났으면 성공 표를 제거하고 data_expired로 반환한다. 기존 관측 시각/기한·snapshot_id를 바꾸거나 재계산하지 않는다. 최종 확인과 전송 사이의 통신 지연 동안 만료될 수 있으므로 브라우저도 표시 시점/복귀 시 만료를 확인하며 기한을 연장하지 않는다.

## 4. 결과 투영안

웹 final의 정확한 최상위 필드는 `status, conditions, issues, supported, squad, snapshot, versions, answer, narration_source, data_mode`다. 내부 kind/code/coverage/evaluated_at/원본 evidence/model_metadata 등은 추가하지 않는다. 새로운 필드를 원하면 계약 버전과 양쪽 검사를 함께 바꾼다.

| 내부 결과 | 전송안 | 자료/모델 접근 |
|---|---|---|
| 준비 input_issues | final needs_clarification, conditions/squad/snapshot=null | 모델·자료·엔진 미호출 |
| 선택 provider_error | error의 안전한 selection_provider_error 또는 등록된 ID 검증 오류 코드 | 자료·엔진 미호출, 재시도/자동 모델 전환 없음 |
| 정상 선택 뒤 missing/ambiguous/unsupported | 기존 정규화 issues로 final needs_clarification | 모델은 사용됐지만 자료/엔진 미호출 |
| 검증된 optimal/feasible | 기존 presentation의 표/조건/합계·snapshot을 선별 복사 후 웹 DTO 재검사 | 최종 만료 확인 필요 |
| infeasible/unknown/data_expired | 같은 status의 final, squad=null | 해 없음/시간 미확정/만료 구분 |
| 기존 처리/검증 internal_error | final internal_error·squad=null, 내부 예외 비노출 | 성공으로 복구하지 않음 |
| HTTP/직렬화/예상 밖 구현 오류 | terminal error internal_error | 원문/경로/stack/모델 응답 비노출 |

v0에 있는 llm_unavailable는 허용 상태 집합의 예약값으로 유지하는 안이다. 첫 live의 선택 실패는 SSE error로 통일하고 같은 실패를 final과 error로 중복 발행하지 않는다. 모델 연결/잘못된 선택을 사용자 조건 누락으로 바꾸지 않는다. 오류 code는 공개 whitelist를 두고 원본 예외 문자열을 그대로 내보내지 않는다.

준비 사유는 `{field,kind,code,message,occurrence}`로 투영한다. code는 `prepare.<기존 code>`로 구분하고 occurrence는 null, span/context_id는 내부에 유지한다. unsupported_reinforcement/unsupported_formation은 unsupported, ambiguous_amount는 ambiguous, 그 외 **등록된** 준비 입력 오류는 invalid로 명시 대응하는 안이다. 미등록 코드/형태는 내부 오류로 거부한다. 정규화 issues는 기존 값을 그대로 유지한다. 이 투영은 웹 설명용이며 ConditionNormalizationResult/normalizer_version이 준비 사유를 생성했다고 주장하지 않는다.

supported는 고정 지원 계약에서 생성하며 모든 final에서 필수다. 준비 차단 시에도 클럽3/포메이션3/강화5~11/필수5조건을 제공한다. 별칭·선수 제외·모델 배치 확장을 덧붙이지 않는다.

BP·가격·급여·합계·spid/person_id는 기존 정수를 정확한10진 문자열로 직렬화한다. snapshot.id는 원래 내용 해시 문자열 그대로다. 작은 정수 OVR/강화와0.5단위 점수만 JSON number다. 성공은 11명·조건·빈 issues·기한 있는 snapshot 필수이며 다른 status에는 표를 싣지 않는다. 지원 범위·동일 인물 제한·합계·독립 검증·원본 만료를 유지한다.

versions는 문자열 값 객체다. web_contract와 preparation/selection/adapter/normalizer/graph/engine/presentation/material 버전을 실제 사용 단계에 따라 기록하는 안이다. 실행하지 않은 단계는 unused, 모델 호출이 없으면 `model:"unused"`다. 호출한 경우 채택 모델명과 고정 digest 및 selector 버전을 기록한다. 기존 presentation의 not_provided를 모델 미사용의 근거로 쓰지 않는다. 준비 오류의 normalizer는 unused이고 별도 웹 투영 버전으로 출처를 구분한다.

narration_source는 첫 live에서 template, answer는 코드 템플릿이다. error는 정해진 안전한 안내만 전달한다. 새 LLM 서술·사실 검사·토큰 스트림은 구현하지 않는다. template을 llm_checked로 표시하지 않는다.

## 5. 준비 상태·동시 실행·연결 종료

기존 GET /health는 Mongo/XGBoost 상태이며 Agent 준비 상태가 아니다. Agent readiness는 명시한 qwen3:8b/digest·규칙 버전·유효한 합성 배치 설정과 실행 의존성을 별도로 확인하는 설계를 제안한다. 브라우저 요청 때 자동 다운로드/서버 시작/모델 전환/합성 배치 교체를 하지 않는다. 외부 tracing 업로드·원문/프롬프트/모델 응답의 웹 전송도 하지 않는다. 모델 상태 조회도 별도 요청이므로 이번 문서 작업에서는 실행하지 않는다.

LLM은 기존120초·재시도0, CP-SAT는10초/1스레드/시드123을 유지한다. 이를 합산한 값만으로 서비스 전체 기한을 보장하지 않는다. 동기 wrapper/엔진을 event loop에서 직접 실행해 스트림을 막지 않도록 작업 실행 계층을 분리해야 한다. 새 worker·HTTP 경계가 기존 도메인 계약을 변경하지 않으며 바인딩/현재 시각/결과는 요청별로 격리한다.

브라우저 abort는 서버 계산의 즉시 취소 보장이 아니다. 연결 종료 뒤 새 단계/전송을 시작하지 않고 진행 중 작업은 종료/기한까지 실행 슬롯을 점유하는 사용자 확정 정책을 따른다. 단일 앱 인스턴스/worker에서 실행 슬롯1개를 관리하며 다중 worker/LAN 배포는 이번 범위가 아니다. Python thread/future 취소를 실행 중 solver의 강제 종료로 보고하지 않는다. 종료 이벤트 전 EOF는 웹 transport_error, 사용자의 중단은 현재 웹 aborted 흐름을 따른다. 자동 재전송/대기열로 이 제한을 우회하지 않는다.

## 6. 구현 분리·검증 게이트

이번 설계 뒤 구현 승인이 있으면 다음 순서의 별도 PR로 진행한다.

1. 순수 HTTP DTO/준비 사유 투영·엄격 필드/버전·성공/실패·정밀도·만료 합성 계약 검사. 모델/DB/새 수집 없음.
2. 주입된 selector/합성 material/clock·제한된 실행 계층으로 HTTP/SSE 연결. TestClient/MockTransport로 분기·순서·동시성·끊김·기한을 검증한다. 실제 모델 호출 없음.
3. 웹 담당이 기준 backend SHA/계약을 읽고 클라이언트 필요 조정. 웹 변경은 별도 PR. 기존 core/graph/presentation을 직접 고치지 않는다.
4. 새로 승인된 실제 호출 범위에서만 로컬 모델→합성 엔진→웹 대조. 승인 상한·기록·오류 중단을 새로 정한다. 이번12회 비교의 남은 승인 없음.

필수 실패 사례는 준비 차단·선택 누락/foreign ID·모델 연결 실패·누락/충돌·해 없음·unknown·내부 검증 실패·최종 전송 전 만료, JSON number로 된 큰 BP/급여 거부, 지연/잘못된/반복 terminal·다른 요청 ID·번호 누락·연결 종료/슬롯 해제다. 오류 때 표를 내보내지 않고 다른 요청/이전 결과가 섞이지 않아야 한다. 브라우저 기존 strict 검사와 실제 backend DTO를 같은 합성 fixture로 대조한다. 현재 이 연결 테스트/실모델 E2E를 통과했다고 보고하지 않는다.

## 7. 운영 결정과 구현 전 남은 항목

사용자 후속 답변으로 앞의 세 항목을 확정했다. 이는 API 코드/자료 생성/서버 실행 승인이 아니다. 나머지는 여전히 운영 제안이다.

| 항목/상태 | 확정값 또는 제안 | 영향/남은 작업 |
|---|---|---|
| Agent 실행 구성 — 사용자 확정 | 같은 저장소/기존 FastAPI 의존성의 별도 합성 Agent 앱·127.0.0.1:8001. 기존8000 카드 API 보존 | 별도 앱/시작 명령·readiness 구현/검증 필요. 웹 담당의 BFF 추가 권한 아님 |
| 실행/끊김 — 사용자 확정 | 동시1·대기0, 초과는 스트림 전HTTP409. 끊김 뒤 새 단계 중단·이미 시작한 작업 종료까지 슬롯 보유 | 단일 앱/worker 전제의 수명주기·슬롯 해제/기한 합성 검증 필요. 즉시 강제 취소 보장 없음 |
| 합성 자료 — 사용자 확정 | seed123·기존 생성기의 members_per_family=3·명시 배치/run_id/observed_at 시작 설정 고정 | 기존 생성기의 클럽×등록계열 단위다. 실제 run_id/시각 값은 운영 입력으로 명시하며 시작마다 자동 교체/시각 갱신하지 않음. 이번 생성0회 |
| 접속 origin — 미정 | loopback 웹 주소1개를 명시한 CORS allowlist, credentials 미사용 | wildcard/LAN/외부 노출은 별도 범위·접속 정책 필요 |
| HTTP 세부 운영값 — 미정 | request_id 1~64자, 본문 바이트 한도·총 요청 기한·readiness DTO는 구현 전 제안/확인 | 허용량·오류/중복 동작이 달라져 클라이언트 계약 검증 필요 |

이 문서는 준비 완료 서버 주소/실행 명령을 제공하는 배포 지침이 아니다. 미정값과 코드 구현 범위를 확정한 뒤 INT-001~005에 실제 버전·PR·SHA·검증을 기록하고 웹 live 연결을 진행한다.

## 8. 구현 재개와 HTTP 정책 확정 (2026-10-20)

사용자가 “다음 단계 진행”을 요청했다. §6 순서에 따라 순수 웹 DTO/준비 사유·결과 투영/요청·SSE 직렬화와 합성 검증부터 구현한다. 이전 설계 전용 제한보다 현재 구현 재개 요청을 우선한다. 기존 core/graph/presentation·웹 파일은 보존하며 새 모델/메타데이터/넥슨 호출·서버 실행·실데이터 작업은 포함하지 않는다.

같은 작업의 후속 사용자 답변으로 아래 값을 확정했다. §2/§7의 해당 제안·미정 표기보다 이 결정이 우선한다.

- CORS origin은 http://localhost:5173과 http://127.0.0.1:5173 두 주소만, credentials=false. wildcard/LAN/외부 노출 없음.
- request_id는 ASCII 영숫자/하이픈/밑줄 1~64자, 본문 최대64KiB. 원문은 기존4,000 코드포인트/그대로 보존. 형식 오류는 모델 호출 전 거부하고 같은 ID의 중복 실행 방지·결과 캐시를 보장하지 않는다.
- 전체 응답 기한150초. 초과하면 안전한 오류로 종료하고 새 단계를 시작하지 않는다. 이미 시작한 작업은 종료까지 슬롯을 점유한다. 기존 모델120초·CP10초·재시도0을 유지하며 thread/future의 즉시 강제 종료 보장이 아니다.

준비 상태 DTO·실제 lifespan/작업 취소의 검사 경계·서버 실행 명령은 다음 연결 증분에서 설계/합성 검증을 구체화한다. 순수 직렬화 코드 완료를 실제 HTTP/SSE·웹 연결 완료로 표시하지 않는다.
