# Stage 3-A 성능 점수 계약 코드

2026-10-19. 사용자가 [성능 계약 설계](stage-3-a-performance-contract.md)에 이어 OVR/2·점수 키 대응·강화 절대 OVR·카드/슬롯 제외와 합성 단위 테스트 구현을 승인했다. 승인 변경은 문서 PR #36에서 먼저 기록했고 기능 코드는 별도 브랜치로 구현했다. scorer 중 **성능 부분**만 구현했으며 배치 허용이나 Stage 3 전체 완료가 아니다.

## 순수 함수와 요청

`FRS/core/squad_recommend/performance.py`의 인터페이스는 다음과 같다.

```text
score_performance(query: PerformanceQuery, *,
                  card: PlayerCard | None,
                  reinforcement: ReinforceStats | None = None) -> PerformanceResult
```

query는 명시 spid·slot·reinforce_level·when을 담는다. D3의 세 포메이션에 있는 슬롯 20종과 강화 5~11만 받으며 현재 입력은 기본 5강 관측이다. 잘못된 요청·추가 필드·지원하지 않는 슬롯·문자열 카드 ID·시간대 없는 시각을 유효한 요청으로 바꾸지 않는다. when은 UTC로 정규화한다. 이 객체는 내부 스냅샷의 기준 시각을 전달하는 계약이며 HTTP 클라이언트에게 과거 시각으로 만료를 우회할 권한을 부여하지 않는다. 3-0/API 연결은 이번 범위가 아니다.

호출자가 카드/강화 관측을 직접 선택해 전달하며 함수는 DB·파일·네트워크·모델을 읽지 않는다. 기본 강화라면 PlayerCard의 OVR만 사용하고 넘겨준 불필요한 강화 관측을 사용했다고 기록하지 않는다. 높은 강화라면 요청 강화·spid·기본 강화·기본 카드 버전이 연결된 ReinforceStats의 **절대 OVR**을 사용한다. 다른 자료 종류의 batch_id가 서로 같아야 한다고 강제하지 않으며 사용한 각 관측의 배치/버전을 그대로 반환한다.

## 결과·제외와 재현

성공 결과는 query·scorer_version·ovr_position·raw_ovr·performance와 관측 참조를 담는다. 관측 참조에는 resource·spid·batch_id·version·source·status·observed_at·valid_until·delete_after가 있다. 카드/강화 객체는 재검증해 가변 stats 등의 잘못된 값을 숨기지 않으며 원본을 변경하지 않는다.

성공 점수는 정확히 OVR/2다. 범위는 1~200이며 201을 100점으로 자르거나 후보별 최소/최대로 재조정하지 않는다. 예를 들어 OVR 137은 68.5다. 승인한 별칭을 항상 적용하고 대응 키가 없으면 직접 슬롯 키나 등록 포지션의 다른 OVR로 대체하지 않는다.

| 제외 사유 | 의미 |
|---|---|
| missing_card | 선택한 카드 관측이 없음 |
| invalid_observation | 공통 문서 계약을 만족하지 못하거나 잘못된 관측 형식 |
| card_mismatch | 요청 spid와 카드가 다름 |
| baseline_mismatch | 현재 기본 5강 관측 계약과 다름 |
| missing_reinforcement | 해당 강화 관측이 없음 |
| reinforcement_mismatch | 카드/목표 강화/기본 강화/기본 버전 연결이 다름 |
| unusable_observation | 사용하는 관측이 unknown·만료·미래 관측 등으로 기준 시각에 유효하지 않음 |
| missing_ovr | 선택한 OVR 키가 없음 |
| ovr_out_of_range | 관측 OVR이 운영 점수 범위 밖임 |

제외 결과는 performance=None과 사유를 반환하며 0점 성공으로 표시하지 않는다. 유효한 요청 안의 데이터 문제는 결과로 반환해 다른 카드·슬롯 평가를 계속할 수 있다. 잘못된 query 자체는 TypeError/ValidationError로 구분한다. 스키마에서 거부하는 음수/잘못된 자료형과 운영 상한 200 초과를 구분하며, 후자는 원시 값도 결과에 보존한다.

`performance_spec()`은 계산식·기준 강화·지원 슬롯/강화·별칭·범위·대체 금지·신선도·배치 미판정 규칙의 새 복사본과 정규 JSON SHA256을 반환한다. 현 규칙 버전은 **f24d6320b03e6751285ccd2b65502c7ff3938f9b3b0deef88412ce1b18ed3c7a**다. 관측 버전·모델 버전과 별개이며 호출자가 사양 복사본을 바꾸어 실행 규칙을 바꿀 수 없다.

## 검증과 남은 범위

합성 테스트 **41개**, 전체 pytest **633개**, Ruff 린트/포맷·환경 import·pip check를 통과했다. 실제 수집 레코드나 실존 선수명/수치는 fixture에 넣지 않았다.

- 경계 1/200·201 제외·정확한 .5 점수·10가지 별칭과 공통 슬롯을 확인했다.
- 강화 7의 절대 OVR 128을 64점으로 계산하고 기본 OVR에 더하지 않음을 확인했다. 기본 강화/다른 강화·spid·기본 버전·독립 배치·강화 만료 경계를 검증했다.
- 누락/상한 초과·관측 오류/미상/만료는 부분 제외되며 원본/정책 복사본이 변경되지 않음을 확인했다. 단위 테스트의 네트워크 차단 및 파일 I/O 금지 아래 점수를 계산했다.
- GK만 등록한 가상 카드라도 CB OVR이 있으면 RCB **점수**는 계산한다. 배치 허용을 확인한 것은 아니며 결과에 placement 판정을 넣지 않는다.

```sh
python -m pytest tests/test_performance.py
```

새 넥슨 요청·DB 접속/적재·실데이터 점수 실행·학습·solver·API 연결은 수행하지 않았다. 등록 포지션 검색과 배치 허용, 모델 확장 임계값/감점, 동일 인물/팀컬러 제약, D4·D5·D6·3-0은 별도다. Stage 1-D·1-F 미완료도 유지한다. 실제 운영 점수나 11명 스쿼드 생성 성공으로 보고하지 않는다. 최신 head의 CI·병합 결과는 PR에서 확인한다.
