# W4-2 별도 합성 Agent HTTP/SSE 앱

2026-10-20 / 데모·Stage4-B/4-C. 기존 카드/포지션 API와 분리한 `FRS.api.agent_app:app`을 구현했다. [웹 계약](agent-http-sse-contract.md)·[DTO 구현](agent-web-contract-implementation.md)의 wire는 web-agent.v0다. 기존 엔진/그래프/presentation·프론트엔드는 수정하지 않았다.

## 구성과 시작 정책

- GET /health: agent-health.v1. 설정/합성 material ID·자료별 배치/관측 시각·가장 이른 기한/시작 시 모델명·digest·Ollama 버전·검증 시각/busy를 반환한다. 미설정·모델 미검증/불일치·미래/만료 자료는503, 준비된 자료는200이다. 모델 검증은 시작 시점 기록이며 health 조회마다 재요청하지 않는다.
- POST /recommend/squad/stream: 엄격 JSON/64KiB/원문4,000코드포인트/request_id ASCII1~64자를 검증한다. 요청 형식422·본문초과413·JSON 아닌 Content-Type415·등록 밖 Origin403·미준비503·동시 초과409를 스트림 전에 반환한다. 읽기 중 기한 초과408, 연결 종료400은 안전한 코드만 포함한다.
- 정확히 두 개발 origin(localhost:5173/127.0.0.1:5173, HTTP)·credentials=false·GET/POST를 허용한다. 수동 검증용 Origin 없는 요청은 가능하며 이 CORS 설정을 사용자 인증으로 해석하지 않는다.
- 명시한 AGENT_RUN_ID/AGENT_OBSERVED_AT/AGENT_MODEL_DIGEST가 필요하다. 모델은 qwen3:8b, seed123/members_per_family3이며 원래 배치/시각을 자동 교체하지 않는다. 설정 누락/검증 실패를 최신값 선택·다운로드·학습으로 복구하지 않는다. 값은 .env에 직접 지정하며 .env.example은 빈 예시다.
- 실제 시작 시에만 최대2개 읽기 전용 Ollama 요청(/api/version, /api/tags)으로 명시 digest를 대조한다. 오류 즉시 검증 중단·재시도0. 유효하지 않은 합성 배치는 모델 조회도 하지 않는다. 후보 원본은 시작 때만 요약하고 요청 수락/health/준비 차단은 캐시한 메타데이터만 사용한다.

실행 명령은 환경/모델 상태 확인 범위를 승인한 뒤 `.venv/bin/python -m uvicorn FRS.api.agent_app:app --host 127.0.0.1 --port 8001 --workers 1`이다. reload·다중 worker·자동 시작·스케줄러는 쓰지 않는다. 이 작업에서 이 명령/상주 서버를 실행한 것은 아니다. 기존8000 API/DB 설정은 유지한다.

## 실행·중단·기한

작업자1개와 요청별 lease를 사용하며 대기열을 만들지 않는다. 수락한 요청은 started0 → 실제 agent 시작 → 실제 render 시작(도달한 경우) → 단일final/error다. 그래프의 사후 stages 목록을 실시간처럼 재생하지 않는다. LLM 서술/토큰 스트림은 없고 코드 템플릿만 출력한다.

전체 응답 기한150초는 본문 수신부터 monotonic 시계로 확인한다. 모델120초·CP10초/1스레드/시드123은 그대로다. 연결 종료/기한 초과는 lease를 중단으로 표시하고 모델 뒤/자료 조회 전후/기존 엔진의 응답 시계 검사 지점에서 후속 진행을 막는다. 이미 실행된 thread/LLM/solver의 강제 종료는 보장하지 않는다. 취소된 asyncio Future를 실제 작업 종료로 간주하지 않고 원래 concurrent Future가 끝날 때까지 슬롯을 유지한다. 그동안 새 요청은409다. 서버 종료 시도 같은 원칙을 따른다.

DTO 직렬화 때 원래 스냅샷 기한을 다시 확인하며, 표 구성 뒤에 만료되면 data_expired·squad=null로 반환한다. 원래 기한을 늘리거나 새 추천으로 복구하지 않는다. 요청/자료/selector/clock/중단 상태는 요청별로 묶고 이전 요청의 결과를 재사용하지 않는다.

## 검증과 범위

신규 합성31개/전체pytest1,440개·Ruff 린트/포맷·환경 import·pip check 통과. TestClient/MockTransport/직접 ASGI2.4 테스트로 실제 합성 CP-SAT/독립 검증/DTO를 연결했다. HTTP 성공·누락/준비 차단·모델 ID 누락·구현 오류/비밀 비노출·요청/CORS·시작 검증/digest 불일치·상태 캐시·만료·느린 본문·기한/끊김 중 슬롯 점유·헤더 전송 실패·후속 자료 차단을 확인했다.

실제 ASGI 응답9개(3클럽×3포메이션)·36개 SSE envelope를 현재 TypeScript parseEnvelope로 읽기 전용 대조해 통과했다(번들Node24.19.0). fixture는 artifacts/web-contract-checks/phase2-http.json에 Git 제외 보관한다. 웹 전체 빌드/브라우저/실제 모델 E2E로 보고하지 않는다.

대역 기반 구현 검증의 실제 모델·메타데이터·넥슨 요청은0회다. 후속 승인으로 수행한 실제 모델 E2E는 아래에 구분한다. 서비스용 상주 서버·브라우저 연결은 아직 실행하지 않았다. Stage1-D/1-F·실데이터/공식 보너스 미완료도 유지한다. 웹 담당은 병합된 backend SHA와 이 계약을 읽은 뒤 클라이언트 조정/실제 연결을 별도 검증한다.


## 승인된 실제 모델 E2E와 재개 검증

2026-10-20 사용자 승인으로 `agent-api-e2e-20261020-001`을 1회 실행했다. ASGI TestClient → 실제 qwen3:8b → 기존 합성 엔진/독립 검증기 → HTTP/SSE 응답을 연결했다. 시작 메타데이터2회·추론3회를 사용했고, 재시도·다운로드·외부 전송·넥슨 수집·8001 상주 서버·브라우저는0회다. 새 승인 상한은 소진됐으며 잠금/원래 실행을 보존한다.

명시 합성 설정은 run_id가 실행 ID와 같고 seed123·members_per_family3·observed_at=`2026-10-20T08:29:12.856974Z`다. 실행 당시 코드 HEAD는 `e723eecaab84e3f094ffc3a4a4156b5daa6604b3`, Ollama0.33.0, 모델 digest는 `500a1f067a9f782620b40bee6f7b0c89e17ae61f686b92c24933e4ca4b2b8b41`이다. 원래 관측/기한을 유지하며 이 과거 검증을 현재 서버 준비 상태나 새 자료로 표시하지 않는다.

| 사례 | 최종 응답 | 확인 결과 | HTTP 전체 시간 |
|---|---|---|---|
| 필수5조건 정상 | optimal·11명 | 원문 조건 일치·정확 문자열 수치·모델 digest·기존 제약 검증 | 11.20초 |
| 급여 누락 | needs_clarification | salary_cap / missing / missing_condition·스쿼드 없음 | 9.66초 |
| 예산 충돌 | needs_clarification | price_cap / ambiguous / conflicting_values·스쿼드 없음 | 10.11초 |

각 사례는 started0·실제 agent/render 진행·단일 final의4이벤트다. 처음 실행 검사기는 정상 사례의 salary_cap을 int로 예상해 실패했다. 실제 wire의 급여는 기존 계약대로 문자열이므로 제품 코드 변경/추론 재실행 없이 저장 SSE를 올바른 타입으로 대조했다. 최초 manifest의 `completed_with_failures` 상태와 파일은 그대로 보존하며 별도 offline-validation의3/3 통과를 구분한다. 2026-10-21 재개 시 최신 develop의 웹 파서로 저장된 실제3스트림/12이벤트를 읽기 전용 대조해 통과했다(Node24.19.0). 이는 브라우저·상주 서버 연결 시험이 아니다.

Git 제외 로컬 보관 위치: `artifacts/agent-api-e2e/approval-20261020-001/`. 승인 잠금·계획·실행/오프라인 검사 스크립트·모델 content·SSE·manifest를 무기한 보관한다. 해시는 다음과 같다.

| 기록 | SHA256 |
|---|---|
| plan.json | 4f9bd68857e67936be11d92b2b606eddd9aec99b2edcd6c4e0b41393c7cb92e0 |
| 원래 manifest.json | 62680f6651c95f07c0bbdaacbe43e2640474a5859beb117c0a32e13cc8efc62b |
| offline-validation.json | 7656f08ffbd58c62ac30e316b523c8ff8e297fd0786c49a98704269fc72e79a6 |

다음 실제 연결은 새 실행 범위로 상주 앱 시작/모델 상태 확인·브라우저 검증을 정해야 한다. 종료한 이번 실행을 다시 돌리거나 관측 시각을 현재로 덮어쓰지 않는다. Stage1-D/1-F·공식 보너스·실데이터 완료 여부는 바뀌지 않는다.
