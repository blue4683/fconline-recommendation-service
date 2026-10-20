# Stage 1-A 데이터 스키마

2026-10-18, 실데이터 경로의 공통 계약이다. D2·D3 사용자 답변과 [MVP 범위](mvp-scope.md)를 반영했다. 네트워크·파일 적재 없이 합성 입력으로 검증한다.

## 구현

`FRS/schema/models.py`에 Pydantic 모델 6종, `FRS/schema/scope.py`에 확정 MVP 범위와 로컬 경로를 정의했다. 이 모듈은 수집·디렉터리 생성·DB 접속·삭제를 실행하지 않는다. 기존 JSON 템플릿과 기존 `player_data` 적재를 자동 전환하지 않으며, 새 파서·저장소 연결은 후속 단계다.

| 컬렉션 / 모델 | 데이터 계약 |
|---|---|
| player_card / PlayerCard | spid·시즌·인물 ID 일치, 이름·기본 능력치의 강화 단계·등록 포지션·능력치·포지션별 OVR. 신체·개인기·주발·특성·사용 기록의 미상 값은 None |
| card_salary / CardSalary | 카드·강화·버전별 급여. ok일 때 양의 정수 필수. 미상 급여는 None이며 합산하지 않음 |
| card_price / CardPrice | 카드·강화·관측 시각별 가격·가격 의미. ok일 때 양의 정수 BP와 확인된 가격 의미 필수 |
| reinforce_stats / ReinforceStats | 강화 차이값, 그 기준 강화·기본 카드 버전, 강화별 관측 포지션 OVR. 차이값은 부호 있는 정수이며 자동 기준값을 추측하지 않음 |
| team_color / TeamColor | 카드·유형·이름별 소속. 클럽·국가·특성·강화를 구분해 덮어쓰기를 방지하는 계약. 추천 지원은 클럽만 |
| crawl_log / CrawlLog | 배치·관측 시각·자원 종류·시도·상태·소요 시간·오류 분류. 차단 403/429는 stopped로 기록. 원문·헤더·자격 증명 필드 없음 |

추천 데이터에는 `schema_version=1`, `batch_id`, `source`, `observed_at`, `delete_after`, `status`, `valid_until`을 두고 카드 데이터에는 `spid`, `version`도 둔다. 수집 로그는 추천 후보가 아니므로 신선도 대신 이벤트 결과를 기록한다. 실제 version·batch 생성 규칙과 참조 문서의 존재 검사는 이후 저장·스냅샷 단계에서 다룬다.

## 보관과 신선도

- `delete_after`는 필수이며 무기한 보관은 **명시적인 null**이다. 필드 누락을 무기한으로 바꾸지 않는다.
- `valid_until`도 필수이며 미확정은 null이다. 신선도 미확정 데이터는 ok로 만들 수 없다. D8의 기간·갱신 주기를 임의로 정하지 않는다.
- 추천에 사용할 수 있는 시간은 `[observed_at, valid_until)`이다. 기한과 같은 시각은 만료로 처리한다.
- 유한한 삭제 기한이 있으면 신선도 기한보다 빠를 수 없다. 두 기한 모두 관측 시각보다 뒤여야 한다.
- 시간대 없는 입력·조회 시각을 거부한다. 저장 시 UTC·밀리초로 정규화하고 MongoDB 클라이언트에 `tz_aware=True`를 적용한다.
- `unknown`, `untradeable`, `stale`은 값이 있더라도 추천에 사용하지 않는다. 미상 값에 0을 채우지 않는다.

`is_usable_at()`은 개별 문서의 사용 가능 여부만 판정한다. 같은 배치·버전의 스냅샷 선택과 문서 간 일관성 검증은 Stage 3-0에서 구현한다. 사용 기한이 지났다고 여기서 DB 상태를 변경하거나 레코드를 삭제하지 않는다.

## 가격 정밀도와 의미

도메인 가격은 부동소수점이 아닌 Python 정수다. BSON에서는 Decimal128로 변환해 64비트 정수를 넘는 BP도 정확하게 보관한다. 모든 정수를 손실 없이 표현할 수 있는 `1..10^34-1` 범위를 계약으로 사용하며 게임의 가격 상한을 의미하지 않는다. BSON의 소수·NaN·무한대는 거부한다.

`price_type`의 reference·transaction은 데이터의 의미를 확인한 후에만 사용할 수 있다. 지금 공개 화면의 현재가 의미가 검증된 것은 아니다. 의미를 모르는 숫자는 `price_type=unknown`, `status=unknown`으로 보관할 수 있지만 추천에 사용하지 않는다. API의 금액 단위·대형 정수 전달 방식은 Stage 2-C·4에서 정한다.

## 포지션과 범위

정규 포지션 코드는 문자열 enum으로 유지하며 좌우 슬롯을 축약하지 않는다. 등록 포지션에 문자열 숫자·음수·SUB·중복을 허용하지 않는다. 공식 메타데이터의 숫자 코드는 먼저 코드명으로 매핑해야 하며, 숫자 토큰 노이즈와 공식 숫자 ID를 같은 규칙으로 제거하면 안 된다.

원본 문서의 강화 값 범위는 관찰한 UI의 1~13을 표현하며 MVP 지원은 5~11로 별도 제한한다. 원본에 국가 팀컬러가 있다는 이유로 추천 조건에 국가를 지원하는 것은 아니다. 확정 포메이션은 4-2-3-1·4-1-2-3·4-2-2-2이며 모두 11개의 서로 다른 슬롯을 가진다. 선수의 배치 허용·팀컬러 발동 규칙은 아직 확정하지 않았다.

## 구현 선택과 검증 근거

Python 3.11 호환 안정 버전을 선정하도록 위임받은 기준에 따라 [Pydantic 2.14.1](https://pypi.org/project/pydantic/2.14.1/)과 전이 의존성을 고정했다. [모델·필드 validator](https://docs.pydantic.dev/latest/concepts/validators/), [시간대가 있는 datetime](https://docs.pydantic.dev/latest/api/standard_library_types/), [BSON Decimal128](https://pymongo.readthedocs.io/en/stable/api/bson/decimal128.html)을 대조했다. JSON Schema는 각 모델의 `model_json_schema()`로 생성할 수 있다.

테스트는 신선도·보관 경계, 무기한과 누락의 구분, UTC·BSON 시간 왕복, 큰 BP 정밀도, 미상 값·가격 의미, 잘못된 ID·포지션, 팀컬러 유형 분리, 강화 기준·MVP 경계, 수집 로그 상태를 검증한다. 실제 선수·가격 데이터는 fixture에 포함하지 않는다.

로컬 Python 3.11.15에서 고정 의존성 설치·pip check, pytest **88개**, Ruff 린트·포맷 전체, 환경 실행 확인을 통과했다. 네트워크 없이 실제 PyMongo 클라이언트의 시간대 옵션도 확인했다. 원격 CI 결과는 이 변경의 PR checks에 남긴다.

코드 커밋 `8eb040d`는 [Linux CI 39109363065](https://github.com/blue4683/fconline-recommendation-service/actions/runs/39109363065)에서도 고정 파일 설치·pip check·pytest·Ruff·환경 실행 확인을 모두 통과했다.

Stage 0의 제공자 허용 확인은 미완료지만, 사용자 자동 후속 진행 요청과 D2·D3 결정에 따라 외부 데이터를 저장·사용하지 않는 공통 스키마만 구현했다. 대량 수집 허용으로 확대하지 않았다. 다음 Stage 1-B에서는 새 파서와 파일럿을 다루며 실제 요청·저장은 기존 외부 수집 제한을 따른다.
