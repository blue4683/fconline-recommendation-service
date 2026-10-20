# R0 강화 능력치 계산기 구현

2026-10-20, 웹 제외 백엔드 재개 요청의 첫 증분이다. [설계](reinforcement-derivation-contract.md)와 [재개 범위](backend-resume-plan.md)의 누적표를 순수 코드로 구현했다. 실제 1강 수집/관측 변환이나 추천 지원 확대가 아니다.

## 구현

- FRS/core/reinforcement.py의 reinforcement_bonus, reinforcement_delta, derive_abilities, reinforcement_spec을 제공한다.
- FRS/schema/abilities.py에 기존 34개 능력치 키를 그대로 공유한다. 학습 입력의 STAT_FEATURES는 같은 튜플을 재사용하며 순서/40피처 해시를 유지한다.
- derive_abilities는 기본 1강 PlayerCard를 다시 검증한다. 34개 키가 정확히 있어야 하며 대상 강화는 bool/문자열/실수로 대체할 수 없는 1~13 정수다.
- 모든 상세 능력치와 존재하는 포지션 OVR만 누적 보너스로 계산한다. OVR은 절대값이다. 누락 키를 채우지 않고, 200 초과를 잘라 맞추지 않으며 기존 정수 범위 초과는 거부한다.
- 결과는 독립 DerivedAbilities의 불변 bytes다. to_dict는 독립 복사본을 반환한다. source_reference에 원본 배치/버전/source/기준 강화/상태/관측 시각/기한/보관 정책을 보존한다.
- source_payload_sha256은 재검증한 PlayerCard의 canonical JSON 해시이며 HTML/파일 manifest 해시가 아니다. rule_version은 규칙/키/경계의 해시, 결과 version은 출처/규칙/대상/값을 묶은 payload 해시다.
- 키·몸무게·개인기·발 등 메타데이터, 가격·급여·자격·모델 확률을 계산하거나 원본 관측을 덮어쓰지 않는다. 반환값을 추천용 ok 관측으로 자동 등록하지 않는다.

## 검증

합성 단위 테스트 34개가 누적표 13단계·5강 대비 차이/역변환·이중 가산 방지·필수 키·잘못된 타입/기준 강화·우회 객체 재검증·정수 범위·unknown/기한 보존·없는 OVR/200 초과·출처 해시·불변 복사·I/O 부재를 검증한다. 기존 피처 해시는 908426caef42b031b7927e10f6a7e1788cfa9aa4edfa9f36c3292e8c55240a31 그대로다.

전체 pytest 1,131개·Ruff 린트/포맷·환경 import·pip check가 통과했다. 신규 매개변수 표기의 pytest 경고를 tuple로 정리하고 해당 34개를 경고 없이 재확인했다. 규칙 버전은 d2fb06d0eb484bc5529666ac7aa9d1c4ef65b40c0be6e3242745d9a98c86285f다. PR/최신 head CI는 완료 후 실제 상태를 기록한다. 현재 테스트는 가상 입력만 사용하며 기존 실데이터와 학습 모델은 읽거나 변경하지 않는다.

## 남은 연결

현재 추천 5~11강과 5강 기준 PlayerCard/ReinforceStats는 변경하지 않았다. 후속 R1에서 명시 합성 1강 자료를 파생 5강 호환 자료와 B(target)-B(5) 차이/절대 OVR로 연결한다. 합성 가격/급여/자격은 별도 규칙/출처다.

실데이터의 1~4/12~13 추가 실측·새 원본/manifest 검증·모델 학습/추론·DB/API·스케줄러는 이번 증분에 포함하지 않는다. R1 자료·Agent 그래프·웹 연결은 아직 구현 완료로 보고하지 않는다.
