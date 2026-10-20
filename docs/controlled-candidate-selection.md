# 정형 후보·ID 선택 C1~C3 구현 결과

2026-10-20 / 데모 W3·Stage 4-A. [구현 계약](candidate-selection-implementation-contract.md)에 따라 순수 후보 생성, 엄격 ID 선택/복원, 기존 그래프 연결을 구현했다. 실제 자연어 모델 성능이나 API/웹 연결을 완료한 증분은 아니다.

## 구현

- `FRS/agent/candidates.py`: 순서 자유인 정형 조건문, 보호한 숫자/강화 쉼표, 값 전체 파싱, 원문 SourceSpan·출현별 값/문맥 ID·불변 registry·원문/내용/규칙 해시. 예산/급여 표식·정식 클럽/포메이션·강화 및 마지막 요청 표현만 받는다. 임의 접두어/부정/정정/알 수 없는 절을 정상 값으로 축소하지 않는다.
- `FRS/agent/candidate_selection.py`: candidate-selection.v1의 필수 ID 배열·엄격 JSON·후보집합 바인딩·원문에서 registry 재생성·외래/중복/교차 필드/누락 ID·전체 커버리지 대조. 모든 출현이 명시 선택된 뒤 서버 SourceSpan을 원래 출현 순서로 복원한다. 값 생성·전 출현 자동 선택·ID 보정·fallback이 없다.
- `FRS/agent/candidate_graph.py`: 준비 사유/모델 오류/기존 Agent 결과를 별도 CandidateRunResult에 구분한다. input_issues 또는 provider_error에서 selector/자료/시계/엔진 경계를 지킨다. 정상 선택만 요청별 고정 추출기로 기존 SquadAgent에 전달한다. 준비 사유를 기존 normalizer 결과로 위장하지 않는다. selector와 그래프를 호출할 때 외부 추적을 비활성화한다.
- `FRS/agent/json_contract.py`: 기존 중복 JSON 키/비유한 값 거부 함수를 공통 모듈로 이동했다. 기존 ollama.strict_json import는 재노출해 유지하며 기존 Ollama 회귀 테스트를 통과했다. 실험 당시 코드/해시·원본 결과를 소급 변경하지 않았다.
- 환경 확인 명령에 새 그래프 import를 포함했다. 새 의존성·모델·외부 서비스는 추가하지 않았다.

원문 4,000자·금액 100자·값 후보 128개·문맥 64개를 강제한다. 첫 문형은 알려진 절당 값 1개여서 문맥 64개가 값 상한보다 먼저 적용된다. 숫자 묶음과 구분 쉼표가 모호하면 유효 접두어를 택하지 않고 명확화할 수 있다. 구분 공백을 권장한다. 입력 문제는 입력 전체를 명확화하고, 잘못된 JSON/후보 ID는 기술 오류다. 길이/상한 초과를 자르지 않는다.

알려진 절의 값 문법 오류는 기존 파서의 사유를 새 준비 결과에 기록하며 모델 전에 중단한다. 필드 누락/반복된 유효 값은 registry에 보존해 ID 선택 이후 기존 정규화가 missing/ambiguous를 판단한다. 모델이 반복된 예산 하나를 버리면 불완전 커버리지 오류가 나며 마지막 값으로 복구하지 않는다.

새 wrapper 결과는 내부 타입이며 HTTP/v0 응답이 아니다. 기존 정상 결과의 AgentRunResult/CodePresentation과 core의 SourceSpan·ExtractedConditions·정규화·스냅샷/CP-SAT/독립 검증·만료 의미는 유지한다. core/·기존 graph.py/presentation.py·API·frontend와 WEB-* 기록은 변경하지 않았다.

## 검증

새 합성 테스트 **84개**, 전체 pytest **1,317개**, Ruff 린트/포맷·환경 import·pip check가 통과했다. 원격 CI는 실제 최신 head에서 확인한다.

84개에는 세 클럽×세 포메이션의 실제 CP-SAT/독립 검증/코드 표 연결, 120개 조건 순열, 정확한 복합 금액/쉼표/강화 목록, 반복/충돌/누락, 부정·정정·미지원/별칭·문형 오류, Unicode와 원문/금액 길이, 64문맥 경계·초과 전체 거부, 128개 ID 출력 한도, registry 변조/다른 요청/외래/중복/교차/누락 ID를 포함한다. 잘못된 Unicode와 과도한 JSON 중첩을 안전하게 거부한다.

문제 경로는 selector·자료·시계·기존 그래프 미호출을 확인했다. 기존 정규화의 누락/충돌도 자료 접근 전에 끝나고, 만료/요청 간 상태 비누적을 유지한다. LANGSMITH_TRACING 환경이 켜져도 selector 내부에서는 tracing=false이고 HTTP 전송이 없음을 대역으로 확인했다. 예상하지 못한 selector 구현 오류는 재시도 없이 드러낸다.

테스트 selector는 registry의 올바른 ID를 반환하는 대역이다. 실제 모델이 ID를 올바르게 선택한다는 검증이 아니다. 과거 고정 5개 중 네 개는 정확한 기존 근거로 복원됐고, 미지원 절이 붙은 입력은 준비 단계에서 명확화했다. 이를 과거 실제 모델 0/5와 비교한 성능 개선율로 계산하지 않는다.

## 버전

- candidate_version: `a3e0701de5b219693768718d792a341a81cc039d609525ae7e48d9f72293dbcb`
- selection_version: `9b10b3e1572b7d5f3dc28292117b69bc5ff5fe1bb73808fac794e2347024a5b0`
- adapter_version: `726738470677ff10c8ba0dba00d0d34911a83cd4a978cf1cc9727143e68a4aa0`

## 남은 범위

실제 Ollama ID 선택 transport/비교·모델 선정(C4), 후보 문형 확장, 입력 명확화의 API/SSE 직렬화·웹 연결, 운영 동시성/취소/시간 정책은 후속이다. 정형 문형에서는 코드가 필드를 이미 결정해 모델의 추가 가치가 작으며 파서 단독 기준과의 효과 비교도 아직 실행하지 않았다.

이번 실제 모델 호출은 0회이고 과거 비교 두 실행·승인 잠금/원본을 보존했다. 새 수집·학습·실데이터·외부 tracing·유료 API·MCP 실행 없음. 1-D/1-F와 공식 보너스·실제 Agent E2E를 완료로 바꾸지 않는다.
