# MF-1 다요소 순수 점수 계산기

2026-10-22 사용자의 설계 기반 구현 요청에 따라 [MF-0 초안](multi-factor-performance-design.md)의 첫 증분을 구현했다. 기준 develop `6c07be70bea0d3209d501bdab6e1ca6346f65367`에서 진행했다. D1 실데이터 경로·기본 합성 실행·1-D/1-F 미완료와 현재 OVR 운영 계약은 유지한다.

## 구현 경계

이번 증분은 **명시 실험 정책 → 순수 점수·구성 근거·제외 사유**다. 정책을 호출자가 제공하는 구조로 구현해 가중치/체형 선호를 운영 정책으로 추측하지 않는다. 설계의 초안 숫자는 [명시 설정 예제](examples/multi-factor-core-policy.json)에만 제공하며 `status=experimental`이다. 계산기는 이 파일을 자동 로드하지 않고 서버 환경/기본값/최신 정책을 선택하지 않는다.

- [multi_factor_policy.py](../FRS/core/squad_recommend/multi_factor_policy.py): 34개 키의 단일 배정, 7개 그룹, 9개 항목, 합100, 기존20슬롯의 완전한 대응, 약발1~5 단조표, 신체 선호 함수의 불변 계약/정규 해시.
- [multi_factor.py](../FRS/core/squad_recommend/multi_factor.py): 정확 유리수 연산, 0.01점 최종 HALF_UP, 프로필별 기여/원시 입력, 정책/입력 해시, 관측 출처, 카드·슬롯 제외.
- [합성 검증](../tests/test_multi_factor.py): 8프로필/20슬롯, 설계 예시, 강화·누락·기한·정수 범위·반올림·신체 선호·정책 변조·입력 불변/I/O 부재.

기존 scorer/스냅샷/CP-SAT·탐욕/독립 스쿼드 검증기/API/MCP/웹은 연결하거나 수정하지 않았다. **현재 서비스 추천은 여전히 OVR 기준**이다. 실제 카드 비교, 관측 파일/DB 접근, 학습·수집·모델 호출·서버/배포도 실행하지 않았다.

## 입력과 계산

`score_multi_factor`는 `PerformanceQuery`, `ScoringPolicy`, `expected_policy_version`, `profile_id`, 명시 카드/필요 강화 관측을 요구한다. 정책 내용과 기대 버전 또는 슬롯 대응이 다르면 자료를 평가하기 전에 오류다. 모든 정책은 호출 시 다시 검증해 `model_copy`/`model_construct` 우회를 막는다. 정책/프로필/곡선/가중치/표는 중첩 tuple과 frozen 모델이며 내보낸 사전 변경은 원본에 영향을 주지 않는다.

초기 비교는 기존 `score_performance`의 OVR 적격성 경계를 재사용한다. 기본5강·추천5~11강·포지션 OVR 조회 키·범위1~200·정확한 강화 연결·조회 시 유효 관측을 유지한다. 배치 허용을 판정하거나 OVR 없는 후보 지원을 확대하지 않는다. 같은 기준 강화는 카드 stats, 다른 강화는 기준 stats+연결된 stats_delta를 정확히 한 번 사용한다. OVR은 목표 강화 절대값을 보존한다.

상세 능력치 정규화 `100×raw/upper`, 그룹/역할 가중 평균, 약발 min(왼발,오른발)의 명시 표를 사용한다. `upper`는 정책 입력이며 예제200은 실험 기준이다. 능력치·원시 수치/양발·체형을 자동 보충하거나 초과값을 클리핑하지 않는다. 활성 그룹의 필요한 키만 요구하고 비활성 GK·신체 항목은 읽지 않는다. 이미 있는 필드의 잘못된 자료형은 기존 관측 스키마 경계에서 거부한다.

`core-stat-foot`은 신체 항목을 모든 카드에서 제외하고 남은 가중치 합으로 나눈다. `body-aware`는 양수 신체 가중치마다 호출자가 H/M/T를 제공해야 한다. 키/몸무게는 명시 오름차순 기준점 사이를 유리수로 보간하고 체형 문자열은 정확 대응한다. 범위 밖 외삽·중립값·유사 문자열 추정·카드별 재가중·다른 정책으로 자동 대체가 없다. 신체 함수의 현실적인 수치는 여전히 미확정이며 테스트에는 가상 선호만 사용한다.

계산 결과의 `score_units`는0~10000 정수, `display_score`는 두 자리 문자열 속성이다. `exact_score`와 각 항목의 value/contribution은 기약분수의 numerator/denominator로 직렬화한다. 화면용 소수 문자열은 JSON DTO에 자동 추가하지 않으며 후속 출력 계약에서 단위를 정해야 한다. 가격/급여·사용률·모델 확률은 계산에 들어가지 않는다.

정책 해시는 설정 내용과 알고리즘 버전/계산 의미를 함께 포함한다. 그룹/가중치/프로필/슬롯/체형 대응의 행 순서는 정규화하며 곡선 기준점 순서와 약발 표 순서는 의미가 있다. 입력 해시는 실제 사용한 카드·강화 내용, 조회·프로필·정책 버전을 포함한다. 기본 강화에서 사용하지 않은 강화 행은 해시에 넣지 않는다.

## 호출 예시: 합성 자료만 사용

다음 코드는 저장소 루트에서 실행할 수 있는 순수 계산 예시다. 설정 파일 읽기는 호출자가 수행하고 scorer 안에는 I/O가 없다. 실제 추천/자료 공급기에 연결하는 예시가 아니다.

```python
from datetime import datetime, timedelta, timezone
from pathlib import Path

from FRS.core.squad_recommend.multi_factor import score_multi_factor
from FRS.core.squad_recommend.multi_factor_policy import ScoringPolicy
from FRS.core.squad_recommend.performance import PerformanceQuery
from FRS.schema.abilities import STAT_NAMES
from FRS.schema.models import PlayerCard

policy = ScoringPolicy.model_validate_json(
    Path("docs/examples/multi-factor-core-policy.json").read_text()
)
when = datetime(2026, 1, 1, tzinfo=timezone.utc)
card = PlayerCard(
    batch_id="synthetic-mf1",
    source="synthetic",
    status="ok",
    observed_at=when,
    valid_until=when + timedelta(hours=1),
    delete_after=None,
    spid=123000001,
    season_id=123,
    person_id=1,
    version="synthetic-mf1-v1",
    name="가상선수_MF1",
    base_reinforce_level=5,
    positions=("ST",),
    stats={key: 140 for key in STAT_NAMES},
    position_ovr={"ST": 128},
    left_foot=3,
    right_foot=5,
)
result = score_multi_factor(
    PerformanceQuery(spid=card.spid, slot="ST", reinforce_level=5, when=when),
    policy=policy,
    expected_policy_version=policy.version,
    profile_id="st",
    card=card,
)
assert result.exclusion is None
assert result.score_units == 6378
assert result.display_score == "63.78"
```

설계 예시 A(OVR128·약발3·그룹70)는6378, B(OVR125·약발5·그룹68)는7369로 재현했다. 실제 선수 품질이나 사용자 선호 일치율의 측정 결과가 아니다.

## 검증과 남은 작업

신규 합성155개가 통과했다. 세부 능력치가 서로 다른 경우의 그룹별 수기 산술, 최종0.005 경계 반올림/서로 다른 분모·Decimal context 영향 없음, 64비트 복원 초과·음수·누락, 선택 강화·유효기한 경계, 키 선호의 상승/하락 구간·역할별 체형 차이, 버전/프로필 오류·중첩 변경 우회·행 순서 정규화를 확인했다. 최초 강화 테스트 기대식의 float/Fraction 타입 오류7건은 정수식으로 수정했다. scorer 오류를 숨기기 위한 기준 완화는 없었다.

로컬 Python3.11.15에서 전체 `python -m pytest` **2,052개 통과(36.94초/4 worker)**, `python -m ruff check .`, `python -m ruff format --check .`(Python273개·Markdown코드예제1개), 환경 import/pip check를 통과했다. 새 예제와 설명은 합성 값뿐이며 문서 링크/사용 예/설계 표와 설정 일치·Git diff도 확인한다. 첫 CI39763434123은2,052테스트/린트 통과 뒤 마지막에 추가한 Markdown 코드 블록 포맷으로 실패했다. 문서 예제를 포맷하고 전체 Ruff/format274개를 다시 통과했으며 수정 head CI는 PR에서 확인한다.

MF-1은 공통 계산 계약의 합성 검증이며 MF-2 선호 세트의 작성·배치 선택·분할·품질 기준, 상세 범위 감사, MF-3 탐색/스냅샷/독립 검증/출력 연결은 후속이다. 운영에 적용할 가중치·체형 함수·새 점수 동점 예산 정책·wire 계약은 아직 확정하지 않았다. 새 실제 추천·수집·학습·모델·DB 실행 승인을 이 증분에서 만들지 않는다.

후속 MF-2(2026-10-22): 이 계산기를 OVR 기준과 같은 판단 자료로 비교하는 평가 계약·도구를 [선호 평가 기록](multi-factor-preference-evaluation.md)에 구현했다. 계산기 동작은 바꾸지 않았다.
