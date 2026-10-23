# web-agent.v2 — 추천 방식 선택 계약

2026-10-23 사용자 결정. 기준 develop `898cb64`. [MF-3 목적 계약](mf3-gk-priority-objective.md)의 명시 모드 `gk_priority_multi_factor`를 웹에서 고를 수 있게 하는 HTTP/SSE 계약이다. 이번 증분은 이 문서와 서버 구현·합성 검증까지다. 웹 화면(`frontend/`)은 웹 담당이 이 문서를 보고 옮기며, 통합 담당은 웹 파일을 수정하지 않는다.

## 1. 사용자 결정

| 항목 | 결정 |
|---|---|
| 방식 선택 | 웹에서 요청마다 선택한다. 화면 기본값은 OVR. LLM이 원문에서 방식을 고르지 않는다 |
| 호환 | `web-agent.v1`과 `web-agent.v2`를 동시에 지원한다. v1 요청의 응답은 지금과 바이트 단위로 같다 |
| v2 응답 | 선수별·합계 다요소 점수, 단계 요약, 정책 출처와 실험 표시 |
| 범위 | 계약 문서 + 서버 구현·합성 검증. 웹 화면은 공용 기록으로 변경 요청. MCP(`mcp-build.v1`)는 OVR만 유지 |

## 2. 방식 목록 `GET /objectives`

기존 `/health`(`agent-health.v2`)는 웹이 모르는 필드를 거부하므로 바꾸지 않는다. 새 엔드포인트가 사용 가능한 방식을 알린다. 모델·자료 호출 없이 시작 시 고정한 설정만 읽는다.

```json
{
  "schema_version": "agent-objectives.v1",
  "default": "ovr",
  "objectives": [
    {"id": "ovr", "available": true, "reason": null, "policy": null, "parameters": null},
    {"id": "gk_priority_multi_factor", "available": true, "reason": null,
     "policy": {"policy_id": "draft-core-stat-foot-20261022", "version": "<64 hex>", "status": "experimental"},
     "parameters": {"gk_exchange": 15, "outfield_tolerance": 5, "gk_tolerance": 2, "search_seconds": 30}}
  ]
}
```

- 다요소 방식은 서버가 정책 파일과 기대 해시를 명시했고(`AGENT_SCORING_POLICY_PATH`, `AGENT_SCORING_POLICY_VERSION`), 두 값이 맞고, 자료 모드가 `observed`일 때만 `available=true`다. 그 밖에는 `available=false`와 `reason`(`not_configured`·`policy_mismatch`·`synthetic_material`)이다. 합성 자료는 양발 정보를 만들지 않아 다요소 점수를 계산할 수 없다.
- 정책 자동 선택·최신 파일 탐색은 하지 않는다.

## 3. 요청

```json
{"contract_version": "web-agent.v2", "request_id": "...", "message": "...", "objective": "ovr"}
```

- `objective`는 v2에서 필수이며 `ovr` 또는 `gk_priority_multi_factor`다. v1 요청(`contract_version: "web-agent.v1"`, `objective` 없음)은 그대로 받는다. v1 요청에 `objective`를 넣으면 기존처럼 422다.
- 사용할 수 없는 방식을 요청하면 실행 전에 HTTP 409 `{"detail": {"code": "objective_unavailable"}}`를 반환한다. 동시 실행 1·대기 0·150초·본문 64KiB·origin·request_id 규칙은 v1과 같다.

## 4. SSE와 최종 응답

- 이벤트 envelope의 `contract_version`은 요청과 같은 값이다. 순서·진행 단계·오류 종류는 v1과 같다.
- v2 `final` payload는 v1 필드에 아래를 더한다. `versions.web_contract`는 `web-agent.v2`다.

| 위치 | 필드 | 의미 |
|---|---|---|
| 최상위 | `objective` | `{id, policy, stages}`. `policy`는 다요소일 때 `{policy_id, version, status: "experimental"}`, OVR이면 `null`. `stages`는 다요소 성공 결과에서 `[{name, status, value}]`(`outfield_ovr`·`gk_ovr`·`multi_factor`), 그 밖에는 `[]` |
| `squad` | `outfield_ovr`, `gk_ovr` | 필드 10명 OVR 합과 GK OVR. 두 방식 모두 |
| `squad` | `total_score_units`, `total_score` | 다요소 11명 합(정수, 1unit=0.01점)과 `"780.79"` 형식 문자열. OVR이면 `null` |
| `squad.rows[]` | `score_units`, `score` | 선수별 다요소 점수(0~10000 정수)와 `"70.85"` 형식 문자열. OVR이면 `null` |

- 기존 `raw_ovr`·`performance`(=OVR/2)·`total_ovr`·가격·급여 필드와 검사는 그대로다. 다요소 점수를 `performance`에 넣지 않는다.
- 서버와 웹은 `score`가 `score_units`의 정확한 두 자리 표기인지, 합계가 행의 합인지, `outfield_ovr + gk_ovr = total_ovr`인지 검사한다. 웹은 점수를 다시 계산하거나 반올림하지 않는다.
- 다요소 답변 문구는 방식과 실험 정책임을 밝힌다. 예: "넥슨 데이터센터 관측 자료 기준으로 필드 우선·GK 단계와 실험 다요소 점수(draft-core-stat-foot-20261022)로 11명을 검증했고 단계별 최적성을 확인했습니다. 가격은 참고가격입니다." 가격 단계 미확정 시 "동일 다요소 점수 내 최대 예산 활용은 시간 내 확인하지 못했습니다."를 붙인다.

## 5. 버전·보존

- v1 경로의 사양 함수(`projection_spec`·`presentation_spec`·`event_spec`·`agent_spec`)와 응답 bytes는 바꾸지 않는다. v2는 별도 사양 해시를 갖는다.
- 다요소 결과는 v1 투영과 MCP에서 계속 내부 오류로 거부한다.
- 실데이터 탐색은 30~40초가 걸릴 수 있다(계약 §9 기준 20~28초 + 조건 추출). 150초 응답 기한 안이며 운영 지연 기준은 아니다.

## 6. 웹 담당 변경 요청 (WEB 후속)

- `GET /objectives`로 방식 목록을 읽고, 사용 가능한 방식만 선택지로 보인다. 기본 OVR.
- v2 요청·응답 파서와 표시: 선수별/합계 다요소 점수(서버 문자열 그대로), 필드/GK OVR 합, 단계 요약, 정책 ID·"실험" 표시.
- v1 파서는 유지한다. 실제 연결 전에 이 문서와 서버 SHA를 공용 기록에서 확인한다.

## 7. 미실행

실제 모델·브라우저 E2E, 실데이터 서버 실행, 웹 구현, MCP 확장, 기본 방식 변경, 정책 가중치 조정, 새 수집·DB 쓰기·배포는 이번 범위가 아니다.

## 8. 서버 구현 (2026-10-23)

- 요청: `StreamRequestV2`(v1 모델 상속, `contract_version="web-agent.v2"`, `objective` 필수). `parse_request`는 `contract_version`으로 고르며 v1 모델은 그대로다. SSE envelope는 요청 버전을 쓰고, 버전이 다른 최종 결과는 기술 오류로 바꾼다.
- 실행: `requested_objective`가 v2 다요소 요청에만 서비스 고정 목적을 넘긴다. `AgentDependencies.objective` → 그래프 → `build_squad_request(objective=)`. 사용할 수 없으면 실행 전에 HTTP 409.
- 출력: `present_agent_result(objective=)`가 다요소일 때 행 점수·합계·단계·정책을 더하고 별도 `multi_factor_presentation_spec`을 쓴다. `_final`이 v1은 `WebFinal`, v2는 `WebFinalV2`(v1 검사 재사용 + 점수/단계/정책 검사)로 검증한다. 스냅샷 정책과 서비스 정책이 다르면 기술 오류다. 만료 시 표와 단계 요약을 지운다.
- 설정: `AGENT_SCORING_POLICY_PATH`·`AGENT_SCORING_POLICY_VERSION`(`.env.example`). `GET /objectives`는 `agent-objectives.v1`.
- 보존: v1의 presentation·projection·event·graph·adapter·engine 사양 해시가 develop과 같다(별도 작업 트리에서 대조). v1 응답은 다요소 설정 유무와 무관하게 같다.
- 검증: `tests/test_web_agent_v2.py` 25개(방식 목록·정책 해시 불일치·합성 자료 차단·v1 불변·v2 OVR/다요소 응답·409·요청 필수 필드·만료·v1 투영 거부·정책 불일치·DTO 변조 8종), 전체 pytest 2,175개·Ruff 통과. 실제 모델·브라우저·실데이터 서버는 실행하지 않았다.
