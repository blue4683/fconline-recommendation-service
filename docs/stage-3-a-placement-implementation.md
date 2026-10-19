# Stage 3-A 기본 배치 계약 코드

2026-10-20. 사용자가 [등록 계열 기본 배치·모델 확장 비활성](stage-3-a-placement-contract.md)을 확정하고 순수 코드·합성 테스트를 승인했다. 범위 변경은 문서 PR #38에 먼저 기록했고 구현은 별도 기능 PR로 분리했다. 현재 경로는 실데이터이며 검증 자료는 합성 관측만 사용했다.

## 인터페이스와 판정

```text
placement(query: PlacementQuery, *, card: PlayerCard | None) -> PlacementResult
```

PlacementQuery는 성능 요청과 같은 명시 spid·MVP 슬롯 20종·강화 5~11·UTC 기준 시각 계약을 재사용한다. 입력은 선택한 기본 5강 PlayerCard이며 강화 능력치나 모델 확률을 받지 않는다. 문서를 다시 검증하고 카드 식별자·기본 강화·기준 시각의 신선도를 확인한다. 잘못된 요청은 TypeError/ValidationError, 관측 문제는 배치 제외 결과로 구분한다.

등록 포지션과 요청 슬롯 양쪽을 승인한 CB·CDM·CM·CAM·ST 좌우 계열로 대응한다. 하나 이상 일치하면 allowed=true·placement_type=base, 일치한 등록 코드 목록을 원래 관측 순서로 반환한다. 예를 들어 등록 LCM/CM/RCM은 요청 RCM에 모두 같은 계열로 대응한다. 모델 확장은 코드의 입력·분기·결과 유형에 넣지 않았다.

배치 결과는 query·placement_version·family·allowed·placement_type·matched_positions·observation·exclusion을 담는다. observation은 카드의 원래 배치/버전/출처/상태/시각/기한 참조다. 허용되지 않으면 type=None·빈 일치 목록·제외 사유를 반환하며 다른 카드/슬롯의 판정을 중단하지 않는다.

| 제외 사유 | 의미 |
|---|---|
| missing_card | 선택한 카드 관측이 없음 |
| invalid_observation | 공통 PlayerCard 문서 계약을 만족하지 못함 |
| card_mismatch | 요청과 다른 spid |
| baseline_mismatch | 현재 기본 5강 관측 계약과 다름 |
| unusable_observation | unknown·만료·미래 관측 등 기준 시각에 사용 불가 |
| unregistered_family | 등록 포지션 중 요청 슬롯과 같은 계열이 없음 |

`placement_spec()`은 독립된 배치 규칙 사양/해시를 반환한다. 현재 버전은 **9913ce3267b7334159e20fe21ded3b487f1bba7fccde3e4d82336b910929998e**다. 배치 계열 표는 점수 별칭과 별도로 보관하고 점수 규칙 변경으로 배치가 자동 확대되지 않게 했다. 사양 복사본의 변경은 실제 규칙에 영향을 주지 않는다.

## 성능·검색·다른 제약과의 경계

- 기본 배치가 허용돼도 해당 강화의 OVR이나 강화 관측이 없으면 성능 점수는 제외될 수 있다. 반대로 CB OVR이 있어 점수를 계산할 수 있어도 등록 계열이 GK뿐이면 RCB 배치는 불가다.
- 이 함수는 목표 강화에 대한 기본 등록 계열 신호이며 강화 능력치·가격·급여·팀컬러·같은 인물 중복·11명 구성을 보장하지 않는다. 공통 문서 구조 검증은 유지하지만 OVR 값으로 배치 적합성을 추정하지 않는다.
- 기존 candidate_search의 정확 등록 일치 및 MongoDB 어댑터는 변경하지 않았다. 계열별 후보 검색/병합과 한 요청의 스냅샷 고정은 3-0에서 연결해야 한다.
- 내부 기준 시각을 받는 계약이며 이를 HTTP 클라이언트의 만료 우회 입력으로 노출하지 않는다. DB·파일·네트워크·모델·solver·API를 호출하거나 원본을 변경하지 않는다.

## 검증

합성 테스트 **52개**, 전체 pytest **685개**, Ruff 린트/포맷·환경 import·pip check를 통과했다. 등록/요청 양방향의 계열 대응과 대칭, 미승인 CF→ST·RF→RW·RWB→RB 등 거부, 여러 등록 포지션, 성능과 배치 분리, 신선도 경계, 요청/관측 오류, 출처/규칙/입력 불변을 확인했다. 모델 확장 입력을 허용하지 않고 파일·네트워크 I/O 없이 판정했다.

```sh
python -m pytest tests/test_placement.py
```

실데이터 판정·새 수집·DB 적재·학습·스냅샷·solver·API 연결은 실행하지 않았다. 성능 점수와 기본 배치의 순수 계약은 구현했으나 전체 추천 엔진 완료는 아니다. 3-0·D4/D6·D5 잔여 동일 인물/팀컬러·1-D/1-F 및 제공자 허용은 미완료다. 최신 head의 CI/병합은 PR에서 확인한다.
