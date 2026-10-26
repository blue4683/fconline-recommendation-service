# MF-2 다요소 점수 선호 평가 계약·도구

2026-10-22 사용자의 MF-2 진행 요청에 따라 [MF-0 설계](multi-factor-performance-design.md) §9.2와 [MF-1 계산기](multi-factor-performance-implementation.md)의 후속 증분을 구현했다. 기준은 develop `77e4fe2079675c16ffb51702e62308bea7f243c0`(#141·#142 병합 후)이다. D1 실데이터 경로, 기본 합성 실행, 1-D/1-F 미완료와 **현재 OVR 기준의 서비스 추천**은 그대로 유지한다.

이번 범위는 **평가 자료 계약, 평가기, CLI, 합성 검증**이다. 실제 관측 파일·DB를 사용한 평가, 사람 판단 수집, 가중치 조정, MF-3 엔진 연결은 실행하지 않았다. 아래 합성 결과는 가상 판단의 산술 검증이며 실제 선수 품질이나 사용자 선호 일치율이 아니다.

## 1. 현재 코드와 설계의 차이 검토

| 확인한 사실 | MF-2 반영 |
|---|---|
| MF-1 `score_multi_factor`는 기존 OVR 적격성(`score_performance`)을 먼저 통과해야 점수를 낸다 | 비교 가능 집합은 OVR과 다요소 정책이 **모두** 두 카드를 점수화한 판단으로 한정한다. OVR 없는 후보의 평가는 MF-D3 미결정으로 남긴다 |
| scorer는 첫 제외 사유 하나만 반환한다 | 정책이 요구하는 능력치·발·신체 필드를 scorer와 별도로 **모두** 확인하는 감사를 추가했다 |
| 관측은 조회 시각에 `status=ok`이고 유효 기한 안이어야 사용된다 | 평가 세트에 `reference_time`을 명시해야 한다. 기한 밖이면 `unusable_observation`으로 제외하며 기한을 늘리지 않는다. unknown·`valid_until=null` 배치는 점수화되지 않는다 |
| 정책 프로필 가중치는 합 100의 정수다 | 항목 제거는 새 정책을 만들지 않고 MF-1의 정확한 구성값에서 다시 계산한 **파생 변형**으로 보고한다 |
| 설계 §9.2는 가격 효과를 분리한 능력 비교와 예산 안 선택을 구별한다 | 판단 기준 `basis`를 기록한다. 이번 평가기는 `ability`만 채점하고 `budget`은 별도 분모(`unsupported_basis`)로 센다 |
| 설계의 8프로필×10쌍=80쌍은 제안이다 | 세트 크기·구성의 기본값을 두지 않는다. 합성 테스트와 예제는 명시적인 가상 판단만 사용한다 |
| 기존 등록 포지션 분류 모델의 확률은 성능 근거가 아니다 | 평가기는 모델·확률을 읽지 않는다 |

## 2. 평가 세트 계약 `mf-preference-set.v1`

[FRS/evaluation/multi_factor.py](../FRS/evaluation/multi_factor.py)의 `PreferenceSet`이다. 모든 모델은 frozen·`extra=forbid`이며 분할 비율·시드·배치·판단자의 기본값이 없다.

| 필드 | 내용과 검증 |
|---|---|
| `set_id`, `schema_version` | 세트 식별자. 버전은 `set_version`(쌍·판단 순서를 정규화한 SHA256)으로 고정한다 |
| `material` | `source`(관측 출처), `player_card_batch`, `reinforce_stats_batch`, `reference_time`. 모든 카드·강화 키가 명시한 배치와 같아야 한다 |
| `policies` | 비교할 정책의 `policy_id`와 `version`(MF-1 정책 해시). 제공한 정책의 내용 해시가 다르거나 정책 목록이 다르면 평가 전에 거부한다. `ovr`은 기준 방식 이름으로 예약한다 |
| `pairs[].pair_id`, `split` | 쌍 ID와 `tuning`(조정용)/`holdout`(보류 평가용) 구분 |
| `pairs[].slot` | 기존 지원 슬롯. 프로필은 각 정책의 슬롯 대응에서 정하며 보고서에 남긴다 |
| `pairs[].a`, `b` | 카드 관측 키(spid·batch_id·version)와 강화. 기준 5강 밖이면 강화 관측 키(batch_id·version)가 필수, 5강이면 금지 |
| `pairs[].judgments[]` | `judge_id`, `judge_kind`(human/synthetic), `verdict`(prefer_a/prefer_b/equal/undecidable), `basis`(ability/budget), `rationale`, `play_style`, `judged_at`, 선택 `game_version` |

추가로 거부하는 경우는 다음과 같다.

- 쌍 ID 중복, 같은 슬롯의 같은 두 카드 비교 중복(순서만 바꾼 경우 포함), 같은 카드·강화끼리 비교, 한 쌍에 같은 판단자의 판단 중복.
- **인물 그룹 누수**: person_id(spid의 하위 6자리)가 조정용과 보류 평가용에 함께 나타나면 거부한다. 같은 인물의 다른 시즌·강화도 같은 그룹이다. 같은 분할 안의 반복은 허용한다.
- 자료 쪽에서는 같은 관측 키의 중복(내용이 같아도)과 세트에 명시한 출처와 다른 관측을 거부한다. 최신값을 골라 충돌을 해소하지 않는다.

판단이 없는 쌍은 허용하며 `pending_pairs`로 센다. 판단 자료가 없다는 사실을 통과로 바꾸지 않는다.

## 3. 평가기와 보고서 `mf-preference-report.v1`

`evaluate_preferences(pset, policies=..., cards=..., reinforcements=..., sensitivity_steps=...)`는 순수 함수다. 파일·네트워크·DB·모델을 쓰지 않는다. 같은 판단 자료에서 OVR 기준(`score_performance`의 raw OVR)과 각 다요소 정책(`score_multi_factor`의 `score_units`)을 비교한다.

| 보고 항목 | 정의 |
|---|---|
| 집계 단위 | 판단 1건. 한 쌍의 여러 판단자는 별도 행이며 불일치를 합치지 않는다 |
| 분리 | `tuning`·`holdout` 분할과 `human`·`synthetic` 판단을 서로 합치지 않는다 |
| 별도 분모 | `undecidable`, `unsupported_basis`(budget), `data_excluded`(어느 방식이든 두 카드 중 하나가 제외), `pending_pairs` |
| 비교 가능 | 위를 제외하고 모든 방식이 두 카드를 점수화한 판단. 모든 방식이 같은 분모를 쓴다 |
| 선호 일치 | `agreement = agree / decisive`. 방식의 동점은 일치가 아니며 `method_tie`로 센다. 반대 선택은 `opposite`(오순위) |
| 동등 판단 | `equal` 판단은 따로 세고 정확히 같은 점수일 때만 `equal_tie`로 본다 |
| 제외 사유 | 방식별 `a:<사유>`/`b:<사유>` 건수. MF-1 사유를 그대로 쓴다 |
| 포지션별 | `by_slot`(슬롯별 모든 방식)과 `by_profile`(정책별 프로필) |
| 오순위 사례 | `cases`: 방식·쌍·판단자·판단·방식 선택·두 값. 다요소 정책은 항목별 기여 차이(a−b, 기약분수)를 포함한다 |
| 항목 제거 | 조정용에서만. 정책별로 활성 항목을 하나씩 빼고 남은 정확 구성값의 가중 평균을 같은 최종 HALF_UP 단위로 다시 계산한다. 결과가 바뀐 판단 수 `changed`를 함께 보고한다 |
| 가중치 민감도 | 조정용에서만. 호출자가 준 정수 단계마다 한 항목의 가중치만 더하고 활성 합으로 다시 나눈다. 음수가 되는 단계는 자르지 않고 `skipped=negative`로 남긴다 |
| 자료 감사 | 참조한 카드·강화마다 정책·프로필이 요구하는 능력치(강화 복원 후)·양발·신체 필드의 `missing`/`missing_delta`/`out_of_range`/`unknown_shape`와 관측 부재를 모두 나열하고 필드별로 센다. 선택 필드(키·몸무게·체형·양발) 부재 수도 따로 센다 |
| 재현 정보 | `evaluator_version`(지표 정의 해시), `set_version`, 정책 버전, OVR scorer 버전, 사용한 관측 내용 해시, 민감도 단계. 실행 시각을 넣지 않으며 `report_version`은 보고서 본문 해시다. 입력 순서와 무관하게 같은 결과를 낸다 |
| 품질 상태 | `holdout`의 사람 판단 중 비교 가능한 판단이 있으면 `measured`, 없으면 `not_evaluated`. 통과 기준이 정해지지 않았으므로 `criteria=null`, `passed=null`이다. 합성 판단 수는 따로 표시한다 |

항목 제거·민감도를 보류 평가용에서 계산하지 않는 이유는 가중치 선택에 보류 평가용 결과가 새지 않게 하기 위해서다. 이 지표 정의들은 모두 **제안**이며 4절의 결정으로 바뀔 수 있다. 정의를 바꾸면 `evaluator_version`이 바뀐다.

정답은 평가 세트의 판단이다. 평가기는 scorer 출력으로 판단을 만들지 않는다. 테스트의 합성 판단은 `judge_kind=synthetic`과 가상 규칙 근거로 표시하며, 점수 기대값은 설계 §7과 같은 수기 산술로 따로 계산했다.

## 4. 사용 방법

순수 함수 호출 외에 오프라인 CLI가 있다. 입력 파일은 호출자가 명시하며 기존 보고서를 덮어쓰지 않는다.

```sh
python -m FRS.evaluation.multi_factor \
  --set docs/examples/multi-factor-preference-set.json \
  --policy docs/examples/multi-factor-core-policy.json \
  --material docs/examples/multi-factor-preference-material.json \
  --sensitivity -4 --sensitivity 4 \
  --output artifacts/multi-factor-evaluation/<실행ID>/report.json
```

- `--material`은 `player_card`·`reinforce_stats` 두 키만 있는 JSON이다. `--material-dir`는 같은 이름의 `.jsonl` 두 파일이 있는 폴더를 읽는다. 어느 쪽이든 세트가 참조한 키만 사용한다.
- `--policy`를 여러 번 주면 정책 여러 개를 같은 판단 자료로 비교한다. 세트의 `policies`와 정확히 같아야 한다.
- 실제 자료의 보고서는 Git 제외 경로(`artifacts/`)에 둔다. 실제 선수별 비교 결과·상세 보고서는 커밋하지 않는다.

[예제 세트](examples/multi-factor-preference-set.json)와 [예제 자료](examples/multi-factor-preference-material.json)는 가상 카드 8장(`가상선수_MF2_*`)과 합성 판단 4건뿐이다. 실행하면 조정용 2건에서 예제 정책 2/2·OVR 0/2, 보류 평가용에서 판단 불가 1건·비교 가능 1건이 나오고 품질 상태는 `not_evaluated`다. 이 숫자는 가상 규칙에 대한 산술 결과다.

## 5. 결정이 필요한 항목

후속 결정(2026-10-22): 사용자가 아래 여섯 항목 모두 권장안을 선택했다. 확정 내용과 실제 준비 결과는 [파일럿 준비 기록](multi-factor-preference-pilot.md)을 따른다. 아래 표는 질문 당시 기록으로 보존한다.

실제 선호 평가를 실행하려면 아래 결정이 필요하다. 답변 전에는 실제 관측 파일·DB 평가를 실행하지 않는다.

| ID | 결정할 항목 | 권장안과 영향 |
|---|---|---|
| MF2-D1 | 사용 배치·관측 사용 정책 | `real-data-20261022-b`(3클럽, `ok` 상태)를 권장한다. 카드·강화 관측의 원래 유효 구간 안에 있는 `reference_time`으로만 오프라인 평가하고, 기한이 지난 뒤의 평가를 현재 추천 허용으로 보지 않는 안이다. 실제 기한 값은 결정 후 읽기 전용으로 확인한다. unknown 배치(예: stage-1-d-enrichment)는 현 scorer로 점수화되지 않는다 |
| MF2-D2 | 비교 쌍 작성과 판단자 | 8프로필×10쌍 제안을 쓸지, 누가 쌍을 고르고 판단할지(사용자 단독/복수), 판단자 ID(가명)·플레이 성향 표기. 쌍을 먼저 고정하고 점수를 보지 않은 채 판단하는 순서를 권장한다 |
| MF2-D3 | 분할 | person_id 그룹 단위로 조정용/보류 평가용을 정한다. 비율과 시드(또는 수동 지정)는 판단 수집 전에 고정해야 한다 |
| MF2-D4 | 지표 정의 | 현재: 판단 단위 집계, 방식 동점은 불일치, 동등은 정확 일치만. 대안: 다수결 합의 단위, 동점 제외, 점수 차 허용 범위 |
| MF2-D5 | 품질 기준 | 통과 기준 수치를 둘지(예: 보류 평가 일치율이 OVR보다 높을 것·최소 표본 수) 측정만 할지. 보류 평가 결과를 보기 전에 정해야 한다 |
| MF2-D6 | 예산 안 판단 | `budget` 판단을 수집할지, 수집한다면 가격·급여 조건을 세트 계약에 추가할지. 현재는 별도 분모로만 센다 |

체형 선호 함수(MF-D2), 정규화 범위·OVR 없는 후보(MF-D3), 새 목적·동점 예산 정책(MF-D4)은 MF-0의 미결정 항목으로 그대로 남는다.

## 6. 검증과 제한

- 신규 합성 테스트 37개([tests/test_multi_factor_evaluation.py](../tests/test_multi_factor_evaluation.py)): 설계 예시의 OVR/정책 반대 선택, 정확한 기여 차이, 분모(판단 불가·동등·budget·자료 제외·대기 쌍), 방식 동점, 정책만의 자료 제외가 모든 방식에서 빠지는지, 기한 밖 기준 시각, 시즌·강화 인물 누수, 배치·강화 키·슬롯·판단자 계약 오류, 정책 버전·목록 불일치, 출처·중복 관측, 민감도 단계 오류, 강화 연결과 누락, 모든 필드 감사, 조정용 전용 항목 제거·민감도, 슬롯·프로필 분해, 사람/합성 분리, 품질 상태, 재현성·순서 무관성, 입력 불변·파일 I/O 없음, 예제·CLI(JSON/JSONL·덮어쓰기 거부)를 확인한다.
- 실제 관측 파일·DB·모델·외부 요청·학습은 사용하지 않았다. 실제 선호 평가 결과는 **미평가**다. 사람 판단 자료는 아직 없다.
- MF-3의 스냅샷·CP-SAT·탐욕·독립 스쿼드 검증기·API·MCP·웹 연결, 새 목적·동점 예산 정책·출력 wire 변경은 하지 않았다. 예제 가중치·상한 200·약발표는 여전히 검토용 가설이다.
- 항목 제거·민감도는 MF-1 결과의 활성 구성값으로 계산한다. 가중치 0인 항목은 구성값이 없어 민감도 대상이 아니며, 정책 전체의 체형을 켜는 비교는 별도 정책(body-aware)을 세트에 함께 명시해야 한다.
