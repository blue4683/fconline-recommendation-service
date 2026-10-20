# W4-1 웹 DTO·결과 투영·SSE 직렬화

2026-10-20 / 데모·Stage4-B/4-C. [HTTP/SSE 설계 §8](agent-http-sse-contract.md)을 순수 코드로 구현했다. 실제 HTTP 앱/포트·수명주기·준비 상태·동시성/끊김은 다음 증분이다. 모델/메타데이터/넥슨 요청과 실제 웹 E2E는 실행하지 않았다.

## 변경

- agent_contract.py: 웹 v0의 정확한 필드/타입·문자열 BP/급여/ID·지원 범위·11명/자리/중복/강화/정확 합계·OVR÷2·상태별 표 유무를 검사한다. 기존 도메인 모델을 변경하지 않는다.
- agent_projection.py: 원문/준비 결과·wrapper/그래프 버전·전체 근거/정규화 결합을 재검사하고 기존 presentation을 호출해 허용 필드만 투영한다. 준비 사유는 prepare.*와 별도 투영 버전을 사용하며 normalizer/model은 unused로 기록한다. 선택 실패는 안전한 기술 오류다. 실제 실행 출처를 명시한 경우만 채택 모델/digest/selector 버전을 기록하고 내부 not_provided로 모델 미사용을 추정하지 않는다.
- 결과는 불변 bytes에 고정한다. 전송용 DTO 검사와 JSON 직렬화 이후 원래 스냅샷의 응답 시계를 확인하고 만료면 표를 없애 data_expired를 반환한다. 원본 기한/스냅샷을 바꾸지 않는다.
- agent_events.py: 원문 보존·엄격 JSON/중복 키·Unicode·ID1~64ASCII·64KiB를 검사하고 모델 호출 전 안전한 오류로 거부한다. 요청별 SSE는 started0/연속seq·실제 agent/render 안내·단일final/error·종료 후 거부를 적용한다. 서버·소켓은 생성하지 않는다.

## 검증

신규 합성55개·전체pytest1,409개, Ruff 린트/포맷·환경 import·pip check 통과. 실제 CP/독립 검증을 사용하는 가상 표, 34자리 BP/2^53 초과 급여, 준비 차단·누락/충돌·ID 누락·다른 원문/위조·출력 변조·만료·JSON/바이트/Unicode/이벤트 수명주기를 확인했다. 단위 테스트는 실제 네트워크/모델을 호출하지 않는다.

별도 읽기 전용 대조에서는 3클럽×3포메이션 성공9개와 명확화3개, 총12개 final/42개 envelope를 현재 frontend TypeScript parseFinal/parseEnvelope에 전달했다. payload·seq/요청ID·종료 종류가 일치했고 추가 필드12개 거부를 확인했다. fixture는 artifacts/web-contract-checks/phase1.json에 Git 제외 보관한다. Homebrew node는 공유 라이브러리 부재로 시작하지 못해 시스템 변경 없이 번들 Node24.19.0의 TypeScript 실행을 사용했다. 이것은 프론트엔드 전체 빌드/테스트 또는 브라우저 E2E 검증이 아니다.

## 경계와 다음 단계

기존 core/graph/presentation·frontend/WEB-*를 수정하지 않았다. web-agent.v0의 wire 호환을 합성 자료로 확인했지만 실제 API/v0 연결 완료를 뜻하지 않는다. HTTP 시작/종료·150초·동시1/대기0/409·origin/CORS·모델 준비 상태 검증은 다음 구현/테스트 대상이다. 서비스용 합성 배치/run_id/관측 시각은 여전히 명시 입력이며 테스트 fixture와 구분한다. 추가 실모델 호출 승인은 남지 않았다.
