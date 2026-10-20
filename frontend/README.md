# FRS 웹 클라이언트

합성 가상 리그 스쿼드 추천의 화면·통신 클라이언트다. 사용자 확정 스택인 Vue 3 + Vite + TypeScript로 만들었다. 기준 문서는 [웹 설계도](../docs/web-agent-blueprint.md), [프론트엔드 지침](AGENTS.md), [공용 기록](../docs/agent-collaboration.md)이다.

현재 상태는 **모의 응답 완료 단계**다. 실제 Agent 채팅 API·SSE 경로는 백엔드에 아직 없고, `web-agent.v0`은 모의 개발용 제안이다. 실제 연결 완료로 보고하지 않는다.

## 환경과 버전

| 항목 | 고정값 | 근거 |
|---|---|---|
| Node.js | 22.x (`>=22.12.0 <23`, `.nvmrc`) | Vite 8·Vitest 5 요구(`^22.12`). 검증 환경 v22.22.0 |
| npm | 10.9.x 이상 | 검증 환경 10.9.4, `package-lock.json` v3 |
| Vue | 3.5.44 | |
| Vite / @vitejs/plugin-vue | 8.3.4 / 6.0.10 | |
| TypeScript / vue-tsc | 5.9.3 / 3.3.13 | TypeScript 7(네이티브)은 vue-tsc가 쓰는 JS API 호환을 확인하지 못해 5.9 계열로 고정 |
| Vitest / jsdom / @vue/test-utils | 5.0.5 / 27.4.0 / 2.4.6 | jsdom 30·test-utils 2.5는 하위 의존성이 Node `^22.22.2`를 요구해 검증 환경(22.22.0)에서 설치 거부 |

`.npmrc`의 `engine-strict=true`로 맞지 않는 Node에서 설치를 막고, 모든 버전을 정확히 고정한다.

## 명령

```sh
cd frontend
npm ci              # lock 기준 설치
npm run dev         # 개발 서버 (Vite 기본 포트)
npm run typecheck   # 앱·테스트 타입 검사 (vue-tsc)
npm test            # 동작 테스트 (Vitest + jsdom)
npm run build       # 타입 검사 후 dist/ 생성
```

웹 CI와 경로 자동 차단은 아직 없다(INT-004). 위 명령은 로컬에서만 실행했다.

## 연결 방식

화면 위쪽에서 **모의 응답** 또는 **실제 연결**을 직접 고른다. 기본 선택은 없다.

- 모의 응답: 서버에 요청하지 않는다. 원문을 해석하지 않고 고른 시나리오를 SSE 바이트로 재생한다. 불규칙한 청크·한글 분할·CRLF·여러 data 행·heartbeat를 섞어 실제 파서를 그대로 통과한다.
- 실제 연결: `VITE_FRS_API_BASE_URL`(예시 `.env.example`)이 있을 때만 고를 수 있다. 제안 경로 `POST {base}/recommend/squad/stream`으로 보낸다. 경로·주소·CORS·포트는 통합 담당의 INT-001/INT-002 확정 전까지 미정이며, 개발 서버 proxy도 설정하지 않았다. 실패를 모의 성공으로 바꾸지 않는다.

브라우저에 노출되는 설정이므로 API 키나 비밀값을 넣지 않는다.

## 요청 형식

백엔드 후보 준비(develop `7c56bbc`)는 쉼표로 나눈 정형 조건문만 받는다. 순서는 자유이고, 마지막에 `(으)로 구성해줘`를 붙일 수 있다.

```
예산 6조, 급여 210, 리버풀, 4-2-3-1, 5~8강으로 구성해줘
```

`예산 6조 BP`, `급여 210 이하`, `리버풀 4-2-3-1`처럼 단위·꾸밈말을 붙이거나 두 조건을 한 절에 쓰면 되묻는다. 화면의 안내와 예시는 `src/contracts/requestFormat.ts`에 있으며, 예시 3개는 위 기준에서 ready를 확인했다(WEB-003). 브라우저는 형식을 검사하거나 원문을 고치지 않는다.

## 구조

```
src/
  contracts/webAgentV0.ts   v0 타입과 구조 검사 (금액은 10진 문자열, 상태별 불변 조건)
  contracts/requestFormat.ts 입력 안내·예시 문장
  services/sse.ts           증분 SSE 파서
  services/agentSession.ts  요청 하나의 스트림 처리 (seq·request_id·종료 이벤트 검사)
  services/liveTransport.ts fetch 기반 POST 스트림. 재시도·재연결 없음
  services/useRecommendation.ts 화면 상태, 중복 전송 차단, 만료 감지
  mocks/                    가상 fixture와 모의 transport
  components/               입력·진행·결과·경기장 배치도·표
tests/                      Vitest 동작 테스트
```

## 지키는 경계

- 엔진·정규화·강화 계산·검증기를 브라우저에 복제하지 않는다. 클라이언트 검사는 받은 결과의 구조·합계·자리·강화 집합을 대조해 **맞지 않으면 거부**할 뿐이며 서버 독립 검증기를 대신하지 않는다.
- 금액·급여·합계·ID는 문자열로 받고 표시한다. JSON number로 온 금액은 거부한다. 비교가 필요한 곳만 `BigInt`로 정확히 읽는다.
- 원문은 trim·치환·요약 없이 그대로 보낸다. 공백뿐이면 보내지 않고, Python 코드포인트 기준 4,000자를 넘으면 자르지 않고 전송을 막는다.
- final 전에는 서버가 보낸 진행 단계만 보인다. 표와 설명은 final에서 한 번에 표시한다.
- 오류·만료·늦은 이벤트를 성공으로 표시하지 않는다. 자동 재전송·재연결이 없고, 진행 중에는 중복 전송을 막는다. 읽기 중단이 서버 계산 취소를 뜻한다고 표시하지 않는다.
- 성공 결과의 `valid_until`이 지나면 만료로 바꾸고 표를 거둔다. 화면 복귀 시에도 확인하며 되돌리거나 연장하지 않는다.
- 이름·설명·오류는 텍스트로만 렌더링한다(`v-html` 미사용).
- fixture의 선수명·ID·수치는 모두 지어낸 값이다.

## 테스트가 다루는 사례

설계도 §6 필수 사례를 기준으로 했다.

- 상태별 표시: optimal·feasible·needs_clarification·infeasible·unknown·data_expired·internal_error·llm_unavailable·스트림 error·프로토콜 오류·종료 전 끊김
- 34자리 BP·2^53 초과 급여 무손실, 숫자형 금액·범위 초과·합계 불일치·상한 초과·자리/인물 중복·허용 밖 강화 거부
- 이모지 포함 원문·4,000 코드포인트 경계·공백 보존·공백뿐인 입력
- SSE: 1바이트 청크의 한글 분할, CRLF/CR, 여러 data 행, 한 청크 여러 이벤트, heartbeat, 잘못된 JSON·버전·type, seq 누락/역전, started 누락/중복, 종료 후 이벤트, 다른 request_id, 빈 줄 없는 마지막 이벤트, 잘못된 UTF-8
- final 전 표·설명 미노출, 실패 시 이전 표 제거, 자동 재전송 없음, 진행 중 중복 전송 차단, 기한 경과 감지와 비가역성
- HTML처럼 생긴 이름·설명의 텍스트 표시, Ctrl/Cmd+Enter 전송, 예시는 입력만 채움
- WEB-003 선택 필드(`code`·`coverage`·`evaluated_at`·`issues[].span`): 생략/null 허용, 범위·순서·중복·형식 오류와 목록 밖 필드 거부, 원문 구간·자리별 후보 수·사유 코드 표시
- live transport: 본문 JSON 전송·query string 미사용·HTTP 비정상/비스트림 응답 구분·1회 호출

화면은 빌드 결과를 headless Chromium으로 열어 데스크톱(1360px)·모바일(390px) 폭에서 정상·되묻기·큰 금액·프로토콜 오류 상태와, WEB-003의 정형 조건문이 아닌 요청·해 없음(자리별 후보 수)·모델 사용 불가(사유 코드) 상태를 확인했다. 좁은 화면에서 표는 가로 스크롤된다.

## 남은 일

- INT-001/002/005: 실제 계약·경로·base URL·CORS가 확정되고 백엔드 PR이 develop에 병합되면 live transport를 맞추고 같은 모의 정답을 실제 합성 Agent 응답과 대조한다.
- INT-003: 서버 동시 실행·취소·시간 상한 정책 확정 전까지 클라이언트는 단일 진행 요청만 허용한다.
- INT-004: 웹 CI(`npm ci`·typecheck·test·build)와 보호 경로 검사는 통합 담당이 추가한다.
