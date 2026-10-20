# W5 로컬 MCP 연결 계약

2026-10-21 / 합성 Agent 데모. 현재 W4 HTTP/브라우저 경로는 PR#79/#80의 develop c0c47bef에서 구현·소량 검증됐고 기능 묶음 PR#81도 생성했다. 다음 W5는 [로드맵](ai-mini-project-roadmap.md) §6의 작은 Tool/Resource/Prompt 경로다. 후속 사용자 답변으로 아래 로컬 stdio/SDK2.3.0·Tool/Resource/Prompt·원문+ID 선택/출력·실행 정책과 합성 검증 구현 범위를 확정했다.

## 목적과 보호 경계

기존 후보 ID 검증·정규화·스냅샷·CP-SAT·독립 검증·출력/만료를 MCP 경계에서 재사용하고, 같은 입력/합성 배치/시각의 직접 호출과 결과를 대조한다. 새로운 추천 알고리즘·자유형 자연어·모델 서술·실데이터 처리를 추가하지 않는다. 기존 HTTP 앱/웹 wire와 엔진 계약도 유지한다.

사용자 확정 범위: 로컬 stdio 서버와 저장소 내부 검증 클라이언트만 제공한다. HTTP MCP 포트·브라우저 연결·Codex/다른 호스트 설정 등록·자동 서버 시작·계정/권한 확장·외부 tracing·새 넥슨/모델 호출은 제외한다. stdio subprocess 통신이므로 stdout은 프로토콜만, 진단은 stderr다. 임의 실행 파일·shell·파일 경로·URL·자료 배치를 Tool 인자로 받지 않는다.

## 현재 확인한 SDK·프로토콜

2026-10-21 [공식 Python SDK](https://github.com/modelcontextprotocol/python-sdk)와 [SDK 문서](https://py.sdk.modelcontextprotocol.io/)는 v2를 안정 계열로 안내한다. [PyPI 배포 메타데이터](https://pypi.org/pypi/mcp/json)는 mcp2.3.0·Python>=3.10을 표시하고 mcp-types2.3.0을 고정한다. 문서의 Python 요구 표기와 README 차이도 있어 실제 배포 metadata·Python3.11.15·기존 고정 의존성의 resolver/설치/import/테스트로 호환성을 확인해야 한다. 이 문서 작성으로 설치·호환 검증을 완료한 것은 아니다.

[2026-07-28 전송 규격](https://modelcontextprotocol.io/specification/2026-07-28/basic/transports)은 stdio와 Streamable HTTP를 구분하며, 현재 규격과 이전 initialize/session 방식은 다를 수 있다. SDK2의 표준 클라이언트/서버 API로 연결하고 실제 협상/버전을 결과에 기록한다. v1 FastMCP 예제를 그대로 복사하거나 특정 handshake를 직접 구현하지 않는다. 최신 draft를 고정 구현 스펙으로 사용하지 않는다.

후보 의존성은 공식 `mcp==2.3.0`(CLI extra 제외)이다. 사용자 승인에 따라 Python3.11용 비사전 릴리스/기존 lock 호환을 검증·고정하며, 충돌을 숨기기 위해 기존 FastAPI/Pydantic/엔진 버전을 임의 변경하지 않는다. SDK 설치 실패·비호환이면 근거를 보고한다.

## 공개할 세 기능 — 사용자 확정

| 종류 | 이름/URI | 입력과 출력 계약 |
|---|---|---|
| Tool | build_squad | request_id·변경 없는 original·candidate-selection.v1 JSON → 검증된 결과 envelope. LLM 추론은 실행하지 않음 |
| Resource | frs://supported-conditions | 기존 SupportedConditions·원문/후보/강화 한도·정식 문형·관련 규칙 버전의 정적 JSON. 자료 생성·시계/모델·엔진 호출 없음 |
| Prompt | select_conditions | original로 기존 prepare_candidates/selection_request를 이용해 후보 ID·바인딩된 스키마·기존 추출 지침을 전달. 준비 문제는 명확화. 생성/표시는 모델 호출·모델 정확도 검증이 아님 |

Tool 입력은 raw 능력치·가격·자격·solver 설정·현재 시각·배치 경로를 받지 않는다. 클라이언트가 선택한 ID를 서버가 원문으로 재생성한 registry와 대조하며 외부 ID/누락/중복/필드 불일치·set_id 불일치는 현재 decode_selection 규칙대로 거부한다. 원문 근거를 우회하는 신뢰된 정규화값 입력은 첫 버전에 두지 않는다. 준비 문제는 기존 CandidatePreparation 사유로 반환하며 자료/시계/엔진 접근 전에 차단한다.

Prompt는 기존 채택 추출기의 후보 선택 지침을 재사용하는 연결 자료다. 모델 호출/다운로드/클라이언트 sampling은 없으며, 반환 Prompt만으로 모델이 자동 도구 선택을 하거나 그래프가 MCP를 사용하게 된 것으로 주장하지 않는다. 첫 검증은 합성 ID 선택 결과를 직접/MCP에 동일하게 공급한다. 실제 Agent 그래프의 전송 교체/웹 연결은 별도 후속으로 구분한다.

## 서버 context와 결과 — 사용자 확정

- 시작 설정은 새로 명시한 run_id/seed123/members_per_family3/aware observed_at의 합성 material과 자료별 batch ID로 고정한다. 기존 SyntheticLeagueSpec/MaterialProvider를 재사용한다. 현재 시각 자동 생성·최신 자료 선택·기한 연장은 없다. startup에서 Ollama를 조회하는 AgentService를 재사용하지 않는다.
- 요청의 t0/응답 clock은 서버가 정한다. 직접/MCP 비교에서는 동일한 주입 clock을 사용한다. 지원 강화5~11·동일 인물 최대1명·선택 클럽11명·D8·OVR/2·CP10초/1스레드/123·정수 정밀도를 유지한다.
- 후보 준비/검증/기존 그래프/코드 presentation을 그대로 재사용하고 기존 HTTP WebFinal과 별개의 mcp-build.v1 envelope를 둔다. 준비 명확화/선택 기술 오류/도메인 결과를 구분한다. schema/type·요청 ID·버전·합성 provenance·원래 만료·정확 BP/급여/ID 문자열을 포함하고 caller 제공 선택을 실제 모델 호출로 표시하지 않는다.
- MCP 오류는 is_error 및 안전한 코드로, 성공 structured_content와 텍스트 JSON은 같은 직렬화 결과에서 만든다. SDK가 출력 스키마를 지원하는 실제 범위를 검증한다. 오류/만료에 스쿼드 표를 포함하지 않으며 직렬화 직전에 원래 기한을 재확인한다.

## 실행·중단·재시도 — 사용자 확정

기존 HTTP 정책과 같은 프로세스별 동시1·대기0·초과busy, 전체 응답150초·solver10초·재시도0·직접 호출 fallback0을 적용한다. MCP가 HTTP409를 반환한다는 뜻은 아니며 프로토콜 오류/도구 오류로 구분한다. JSON-RPC ID와 애플리케이션 request_id는 별도이며 request_id는 상관관계만 제공한다. 동일 request_id의 재전송을 중복 방지/캐시로 처리하지 않는다.

취소 notification/연결 종료/기한은 후속 단계 시작을 막고 진행 중인 실제 thread/solver 종료까지 실행 슬롯을 유지한다. SDK task 취소를 물리 작업 종료로 취급하지 않는다. 강제 취소·프로세스 장애 시 exactly-once를 보장하지 않는다. Resource/Prompt는 정적인 읽기/구간 준비만 하며 solver 슬롯이나 모델 호출을 사용하지 않는다.

요청 경계는 기존 original4,000코드포인트·request_id ASCII1~64·후보128/문맥64를 유지한다. 프로토콜 메시지 전체/SDK 수신 버퍼와 앱 인자 검사는 별개이므로 64KiB 애플리케이션 제한을 SDK 전송 전체의 DOS 방어로 과장하지 않는다.

## 구현·검증 게이트

1. 사용자 결정 기록 → SDK2.3.0 호환성/잠금 확인 → 별도 기능 브랜치에서 순수 DTO·주입 서비스·stdio server/client 구현.
2. 정적 기능 목록·Prompt 원문 바인딩·미지원/상한·원문 보존·준비/선택 오류·누락/충돌·성공11명·infeasible/unknown/만료·큰 수를 대역/합성으로 검사한다. Resource/Prompt·오류 입력에서 자료/엔진/모델 접근이 없는지 확인한다.
3. 동일 고정 요청/배치/clock의 직접/MCP 결과 대조, 클라이언트/서버 오류·기한/끊김/취소·busy·실제 종료까지 슬롯 유지·자동 retry/fallback 없음·stdout JSON-RPC 오염 없음·subprocess 종료를 검증한다.
4. pytest/Ruff/환경/pip와 최신head CI 후 develop Squash 병합·완료 브랜치 정리·기존 master 대상 PR 갱신 또는 새 기능 묶음 PR 생성. master 병합/배포는 하지 않는다.

이 단계의 합성 대역 통과를 실제 모델 tool-use·추출 정확도·운영 MCP host 호환성·전체 W6 평가 완료로 표시하지 않는다. W6의25문장/반복/기준, 모델 서술 검사, 외부 tracing, 제출 날짜는 별도 미확정이다.
