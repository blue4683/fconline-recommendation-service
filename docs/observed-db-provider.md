# 로컬 MongoDB 관측 자료를 Agent에 공급하기

2026-10-22 사용자 다음 단계 요청에 따라 실데이터 RD-3의 DB 사본을 Agent가 읽는 공급기를 추가했다. 기존 [DB 적재 검증](observed-db-load-validation.md)은 저장·검색 검증이고, 이번 증분은 Agent와 HTTP 연결까지다. 실모델·브라우저 E2E는 별도 실행 범위로 남긴다.

## 선택과 무결성 계약

- `AGENT_MATERIAL_SOURCE`는 빈 값/`file`(기존 기본값), `mongodb`만 받는다. `mongodb`는 `AGENT_DATA_MODE=observed`에서만 허용한다. 합성 모드의 자료 생성과 기존 파일 공급기를 유지한다.
- `AGENT_RUN_ID`와 자료 종류별 배치는 하나의 완료된 실행을 명시한다. 최신 실행을 찾거나 다른 배치로 대체하지 않는다. 관측 시각 설정은 비우고 원래 행에서 읽는다.
- 파일은 원본 기준으로 남는다. `MongoObservedMaterial`은 기존 `ObservedMaterial`로 보고서·가공5파일 해시·출처·배치를 검증하고, 전체 typed model의 건수와 정규 SHA256을 고정한다. 이때 DB에 연결하지 않는다.
- `load()`마다 원본 보고서와 파일 해시를 다시 확인하고, 로컬 DB에서 명시 배치의5컬렉션을 읽는다. 각 행을 기존 스키마로 복원하고, 순서와 Mongo `_id`를 제외한 **모든 행의 모든 필드와 중복 수**가 원본 기준과 같은지 대조한다. 기준 파일의 행을 응답 대신 반환하지 않는다.
- 읽기 전후 `observed_load`가 정확히1건이고 run-id·material ID·가공해시·컬렉션별 건수가 원본과 같아야 한다. 행 조회는 원본 건수+1로 제한해 초과 행도 거부한다. unknown·만료·충돌을 status 필터로 숨기지 않는다. 다른 배치의 자료는 조회하지 않는다.
- 누락·추가/중복·값/시각/출처 변경·스키마 오류·DB 장애이면 중단한다. 파일 fallback·자동 적재·인덱스 생성·수집·재시도는 없다. 기본 연결은127.0.0.1/localhost/::1만 허용하고 드라이버 `retryReads=False`를 명시한다. 공유 DB Client의 기존 기본 동작은 유지한다.
- DB 연결과 cursor는 성공/오류 모두 닫는다. 계정 권한이나 DB 설정을 변경하지 않고 `find`만 사용한다. 비밀번호·드라이버 예외의 상세 주소를 응답에 넣지 않는다.

기존 스냅샷·CP-SAT·독립 검증기·응답 직전 만료 검사를 그대로 사용한다. unknown 가격과 원래 관측 시각·기한을 바꾸지 않는다. `loaded_at`을 관측 시각으로 쓰지 않는다.

## 시작 설정과 응답

```dotenv
AGENT_DATA_MODE=observed
AGENT_MATERIAL_SOURCE=mongodb
AGENT_RUN_ID=real-data-20261021-b
AGENT_OBSERVED_AT=
```

기존 `AGENT_MODEL_DIGEST`와 로컬 `MONGO_*` 설정도 필요하다. 원본 파일 기본 위치는 실행 체크아웃의 `data/processed`다. 다른 자료 체크아웃을 읽을 때는 기존 `build_service(..., processed_root=Path("/path/to/data-checkout/data/processed"))`를 서비스 팩토리에서 명시한다. 자료를 복사하거나 최신 경로를 추측하지 않는다. `mongo_client_factory`는 테스트/명시 로컬 연결을 주입하는 내부 경계이며 HTTP 입력으로 받지 않는다.

시작 때 DB 준비·무결성 검증을 먼저 한다. 실패하면 `/health`는 기존 `not_configured` 503이며 모델 검증에 도달하지 않는다. 준비된 자료가 만료됐으면 `data_expired` 503이고 모델 상태를 확인하지 않는다. 정상 실제 시작의 모델 metadata 요청2회는 기존처럼 새 승인 범위가 필요하다.

`/health`는 기존 **시작 시 자료·모델 검증 스냅샷**이다. 매 health 조회마다 DB를 읽지는 않는다. 시작 후 DB 장애/변조는 정상 조건 요청의 material load에서 검출해 안전한 `internal_error` SSE로 종료하며 스쿼드를 반환하지 않는다. health ready만으로 현재 DB 연결이 정상이라고 해석하지 않는다. 잘못된 입력의 명확화는 요청 중 DB/엔진 접근 전에 끝난다(시작 준비 때의 읽기와 구분).

웹 wire는 `web-agent.v1`·`agent-health.v2`·`data_mode=observed`를 유지한다. 파일/DB의 material ID와 배치·관측 시각·기한이 같으므로 같은 자료의 provenance도 같다. 공급기 선택은 내부 설정이며 웹 응답에 DB 주소·계정·파일 경로를 추가하지 않는다.

## 검증 결과

합성 테스트는 DB 읽기 전용 대역과 합성 관측 형식만 사용한다. 전체 행·unknown·큰 정수BP·문서 순서·다른 배치 분리, 행 누락/중복/변조·보고서 변경·적재 기록 누락/중복/불일치/읽는 중 삭제, 드라이버 연결/읽기 오류·연결 종료·재시도 금지, 설정/기본 파일 모드·시작 실패·만료·HTTP 안전 오류를 검증한다. 3사례의 전체 최종 wire를 파일 공급기와 대조한다.

실제 로컬 대조 `observed-db-provider-20261022-002`는2026-10-22 00:23:41~00:23:52 KST에 completed다. 기존 data 체크아웃의 `real-data-20261021-b`와127.0.0.1:27017/frs를 직접 읽었다. **모델 검증·조건 선택은 대역**이며 TestClient를 사용했다. 실제 서버·브라우저·Ollama metadata/추론·새 수집·LangSmith·DB 쓰기0회다.

| 사례 | 파일/DB 공통 결과 | 해당 요청 DB load |
|---|---|---:|
| 예산13만·급여200·리버풀·4-2-3-1·5강 | optimal11명·37,220 BP·급여200·OVR1,174 | 1 |
| 위 요청의 급여 누락 | needs_clarification·스쿼드 없음 | 0 |
| 예산13만과5만 충돌 | needs_clarification·스쿼드 없음 | 0 |

두 공급기의 health와 3사례 최종 wire가 같았다. 별도 도메인 실행의 원래 스냅샷·11명 선택을 독립 검증기로 다시 확인해 통과했다. DB 공급기는 시작 준비3회(검증 전/후 서비스 구성 및 대역 선택기 연결)·정상HTTP1회·독립 도메인1회, 합계5연결/5종료·find35회였다. 자료35,948건·unknown 가격91·적재 기록1건과 원본 해시는 전후 불변이다. 원래 가격 기한은2026-10-22 17:03:29.357 KST다.

초기 검사 `observed-db-provider-20261022-001`은 검사기의 `13만 BP`가 기존 금액 계약에서 `missing_coefficient` 명확화로 차단됐으나 검사기가 optimal을 예상해 중단됐다. 제품 파서를 바꾸지 않았으며 실패 manifest/잠금/검사기를 보존한다. 002는 기존 E2E003의 정확한3문장을 사용하고 응답을 판정 전에 저장했다. 001을 성공으로 바꾸거나 수집/모델 승인을 재사용하지 않았다.

증거는 주 체크아웃의 Git 제외 `artifacts/observed-db-provider/observed-db-provider-20261022-002/`에 있다. completed manifest SHA256은 `91ebfda11ca6d9507fa126748e2c74ae382214560a2937eeca42662b0d88e8d7`다. 검사기는 `artifacts/handoff/observed-db-provider-check-20261022.py`이며 종료 실행을 덮어쓰지 않는다. 관측 행·실제 응답·자격 증명은 커밋하지 않는다.

다클럽 PR#112·수집기 상한 PR#113의 최신1f1e385를 병합한 기준에서 **전체 pytest1,775개(87.07초)**·Ruff·format254파일·환경 import/pip 검사가 통과했다(Python3.11.15). 이번에 추가한 합성 검증은30개다. 로컬 DB 대조 뒤 추가 병합한 변경은 수집기/문서/테스트에 한정됐으며 공급기·Agent·DB·엔진 코드는 동일하다. PR 최신 head CI는 별도로 확인한다. 웹 파일/계약은 같아 웹 검사는 재실행하지 않았다. 실제 qwen3:8b+DB+브라우저 검증은 미실행이다. 이전 파일 기반 E2E003 완료와 소진 승인을 유지한다. 이번 작업은 `feat/real-data` 전용이며 master/develop·웹/엔진 코드·다른 담당의 다클럽 작업·기존5173 v0를 보존한다.
