# Stage 4-A 필수 5조건 정규화 구현

2026-10-20, [사용자 승인 계약](stage-4-a-conditions-contract.md)에 따라 실데이터 경로의 공통 모듈을 수동/합성 근거로 구현했다. 새 외부 요청·LLM·DB/API·실제 스쿼드 실행은 0회다. 1-D·1-F와 Stage 4-A 전체는 미완료로 유지한다.

## 입력과 출력

`FRS/core/request_normalization/conditions.py`와 패키지 공개 함수로 제공한다.

- `ExtractedConditions`: price_cap·salary_cap·team_color·formation·allowed_reinforcements의 원문 구간 목록과 unsupported 목록. 빈 목록은 누락이며 조건 기본값이 아니다. 숫자로 변환한 LLM 결과를 받지 않는다.
- `normalize_conditions(original, extracted)`: 원문 길이/추출 구조·모든 구간을 먼저 검증한 뒤 필드별 정규화를 수행한다. 원문 불일치 구간은 파싱하거나 다른 위치로 복구하지 않는다.
- `ConditionNormalizationResult`: ready 또는 needs_clarification, 완성 conditions 또는 None, 모든 issues, 정확히 대조된 evidence, 현재 supported 목록, normalizer_version.
- `ConditionIssue`: field·kind(missing/invalid/unsupported/ambiguous)·code·안전한 한국어 message와 원래 추출 목록의 occurrence 인덱스. 원문 내용은 메시지에 삽입하지 않는다.

issues가 하나라도 남으면 conditions=None이다. 잘못된 값이나 미지원 조건을 지운 부분 요청을 반환하지 않는다. evidence는 구간이 검증된 원문이며 값의 문법이 잘못된 경우에도 보존한다. 구간 자체가 틀린 근거는 evidence에 넣지 않는다. occurrence는 입력 목록 인덱스이므로 잘못된 구간을 제외한 evidence의 위치와 같다고 가정하지 않는다.

완성 conditions는 다섯 필드만 갖는다. 배치 ID/기준 시각/모델/스냅샷을 자동 선택하지 않고 SnapshotQuery·solver·API와 연결하지 않는다. 반환 DTO/입력은 불변이며 재검증해 생성자 검증 우회를 거부한다. 파일·로그·네트워크 저장/호출도 없다.

## 값과 충돌

예산은 기존 정확 BP 파서를 재사용한다. 급여는 올바른 세 자리 쉼표 묶음의 ASCII 비음수 정수이며 기존 0~2^63-1을 지킨다. 단위/소수/부호를 버리지 않는다. 길이가 긴 숫자는 정수 변환 전에 범위를 확인한다.

강화는 5강·5~11강·5강,7강 문법과 기호 주변 공백을 지원한다. 범위는 양 끝을 포함하고 각 끝의 지원 범위를 확인한 뒤 확장한다. 각 목록은 중복을 제거해 오름차순 튜플로 반환한다. 역순/빈 항목/혼합 범위 목록/다른 구분 기호/지원 밖 강화는 오류이며 일부만 채택하지 않는다.

클럽3종과 하이픈 포메이션3종만 정확 대응한다. 앞뒤 공백은 구간 대조 뒤 처리하며 내부 공백/문자/별칭을 고치지 않는다. 동일 필드가 반복되면 같은 정규값인 경우 모든 근거를 유지한다. 서로 다른 값은 conflicting_values로 보고하며 마지막 값 선택이나 서로 다른 강화 필드의 합집합을 사용하지 않는다.

규칙 버전은 `conditions_spec()`의 정렬 JSON 해시이며 금액 규칙 버전·길이·범위·문법·공식 지원값·충돌/오류 정책을 포함한다.

`c83756caefaa6c33bee73d5b5c8b57ac1646187d6d4d5b446e93147054763e33`

## 검증

새 합성 테스트120개 포함 전체 pytest **1,051개 통과**. 정상 클럽/포메이션 조합·강화 경계/범위/목록/중복·급여 쉼표/큰 정수·0/34자리 BP·필드별 누락·여러 오류 동시 보고·잘못된 근거를 파싱하지 않음·반복/충돌·별칭/미지원·정확한 원문/불변·검증 우회·파일/네트워크 부재를 확인했다. Ruff 린트·포맷, 환경 import, pip check도 통과했다.

필수 명령은 python -m pytest, python -m ruff check ., python -m ruff format --check .이며 고정된 .venv 환경을 사용했다. 최신 head의 Linux CI는 PR에서 별도 확인하고 실제 병합 상태를 보고한다.

## 한계와 다음 결정

이 모듈은 수동/합성 추출 결과를 검증하며 원문 전체에서 필드를 찾는 추출기가 아니다. 추출기가 보고하지 않은 미지원 절이나 잘못된 부정 해석/필드 분류는 원문 구간 일치만으로 찾을 수 없다. 이 한계를 합성 테스트에도 명시했으며 의미 정확도·E2E 성공으로 보고하지 않는다.

LLM 공급자/모델·자격 증명/비용·재추출 횟수와 실제 추출, build/표/서술·DB/API 연결은 후속이다. 새로운 수집·실데이터 실행·1-D/F 완료나 공식 팀컬러 발동/보너스 승인은 아니다.
