# FRS 합성·관측 웹 클라이언트

Vue 3 + Vite + TypeScript 화면·통신 클라이언트다. 확정 계약은 `web-agent.v1`과 `agent-health.v2`이며 합성/관측 자료를 구분한다. 2026-10-22 사용자 요청으로 feat/real-data → develop → master에 같은 v1 소스를 통합한다. 실행 중인 기존 v0 서버를 교체하는 작업은 포함하지 않는다. 기준은 [실데이터 설계](../docs/real-data-branch-design.md) §7·§12.2와 [공용 기록](../docs/agent-collaboration.md)의 INT-RD-001·WEB-008이다.

## 환경·명령

기존 lock과 의존성 버전을 유지한다. Node 22(`>=22.12.0 <23`), npm 10.9 이상이 필요하다. 이번 검증은 Node 22.23.3·npm 10.9.9·Python 3.11.15다. 기본 Node가 다른 버전이면 Node 22의 bin 디렉터리를 PATH 앞에 둔다.

```sh
cd frontend
npm ci
npm run typecheck
npm test
npm run build
```

Vue 3.5.44·Vite 8.3.4·TypeScript 5.9.3·Vitest 5.0.5 등 정확한 버전은 package.json/lock을 따른다. 이번 작업은 새 의존성을 설치하지 않고 같은 lock의 기존 로컬 의존성을 복사해 검증했다.

## 자료 모드와 표시

- 연결 방식은 모의 응답/실제 연결 중 명시적으로 고른다. 모의 응답은 항상 합성 가상 자료다. 실제 연결의 자료 모드는 검증한 서버 응답에 따른다. 모드를 바꾸면 이전 결과를 지운다.
- 관측 자료에는 넥슨 데이터센터 관측 자료·관측 실행 ID(`/health.material.batches` 값)·관측 시각·유효 기한·참고가격/거래 보장 아님을 표시한다. 가격은 현재 기준 절대 BP다. 배율을 곱하거나 나누지 않는다.
- `/health.material.observed_at`은 자료의 관측 시각이다. 결과의 `snapshot.t0`은 요청 기준 시각이므로 관측 시각으로 표시하지 않는다.
- 관측 모드의 해 없음은 보유한 관측 자료의 범위로 설명한다. 수집하지 않은 클럽까지 게임 전체에서 해가 없다고 단정하지 않는다.
- 서버가 자료 만료/모델 미확인/설정 실패를 반환하면 보내기를 막는다. 브라우저 시계의 기한 경과는 안내이며 최종 판단은 서버가 한다. 반환된 스쿼드가 만료되면 표를 거두고 기한을 늘리지 않는다.

## 연결과 계약

`VITE_FRS_API_BASE_URL`이 있을 때만 실제 연결을 고를 수 있다. 경로는 `POST {base}/recommend/squad/stream`, `GET {base}/health`다. Vite는 허용 origin인 localhost/127.0.0.1의 5173 포트만 사용하며 다른 포트로 자동 변경하지 않는다. 브라우저 설정에 비밀값을 넣지 않는다.

- 요청/이벤트와 versions.web_contract는 `web-agent.v1`이어야 한다. v0는 수용하지 않는다.
- started/final의 data_mode는 synthetic/observed만 받으며 한 스트림에서 서로 다르면 결과를 거부한다.
- health는 agent-health.v2다. not_configured일 때만 data_mode=null이고, 나머지는 명시한 자료 모드가 필요하다.
- 원문·금액·ID·급여 문자열과 기존 엄격 검사를 유지한다. 값을 추측 보정하지 않는다. SSE/모델/전송 오류를 모의 성공으로 바꾸거나 재시도하지 않는다.
- 상태 조회는 실제 연결 선택 시1회·다시 확인 버튼·요청 종료 후1회뿐이다. 주기 조회는 없다.

코드는 src/contracts/webAgentV1.ts·agentHealth.ts, services/의 세션/상태/전송, components/의 표시로 나뉜다. 엔진·정규화·서버 API는 수정하지 않았다.

## 합성 대조 fixture

저장소 루트에서 다음 명령으로 다시 만든다.

```sh
PYTHONPATH=. python frontend/tests/fixtures/generate_backend_fixture.py > frontend/tests/fixtures/backend-agent-app.json
```

v1 앱을 FastAPI TestClient로 호출한다. 기존 합성 리그를 임시 파일에 관측 형식으로 쓴 모드도 포함하지만, 이름·ID·가격·급여는 전부 가상 값이다. 실제 수집 파일을 읽거나 모델/DB/외부 네트워크/상주 서버를 호출하지 않는다. 생성기는 소켓 연결을 차단한다.

기준 backend SHA는2998c1c다. SSE12건(합성7·관측 형식5), 스트림 전 거절6건(v0 포함), health7건을 기록한다. 1/7바이트 재생과 파서·세션·화면을 검증한다. 상태별 모드/null 조합·버전 거부·정확한 수치·만료·모드 전환 시 이전 표 제거도 검사한다. 이 결과는 실제 qwen3:8b나 실제 관측 자료의 브라우저 E2E 성공을 뜻하지 않는다.

## 실행 경계

당시 웹 타입 검사·125테스트·빌드를 통과했고 이후 최신 검증은 [통합 기록](../docs/real-data-mainline-integration.md)과 공용 기록/PR을 따른다. 웹 자동 CI는 아직 없으며 로컬 검사와 구분한다. Python CI는 develop·master·feat/real-data PR/push를 대상으로 한다.

실제 모델·metadata·Ollama/Agent/Vite 임시 실행·브라우저 E2E는 새 실행 범위와 상한을 승인받은 뒤 통합 담당이 수행한다. 3클럽 real-data-20261022-b 가격은2026-10-23 08:27:17 KST에 만료된다. 기존 리버풀 배치의 기한은 보존한다. 재수집·시각/기한 변경·소진 승인 재사용은 하지 않는다. 소스 병합은 서버 기동·새 실제 추천·배포 승인이 아니다.
