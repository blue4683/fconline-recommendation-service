# Stage 2-C 읽기 전용 API

2026-10-19 사용자 승인으로 1-D·1-F 미완료를 유지하고 API를 먼저 구현했다. 선행 예외는 별도 문서 PR #33에서 AGENTS.md·설계도 §5/§11에 기록했다. 현재 경로는 실데이터이며 이번 정상 추론 검증에는 가상 선수·가상 수치만 사용했다. 새 넥슨 요청·실데이터 적재·모델 자동 선택은 없다.

## 실행과 설정

FastAPI 0.147.0, Uvicorn 0.54.0, 개발 테스트용 HTTPX2 2.14.0을 Python 3.11.15에서 검증·고정했다. 기존 의존성 버전은 유지했다. Python 3.11 메타데이터·비사전 릴리스 여부를 PyPI에서 확인하고 깨끗한 가상환경에서 설치·의존성 검사를 통과했다. 설치용 두 requirements 파일에 전이 의존성도 고정했다. Starlette 1.9.0이 안내하는 HTTPX2를 사용해 이전 HTTPX 호환 경고를 해소했다.

근거: [FastAPI 배포 안내](https://fastapi.tiangolo.com/deployment/manually/), [컨테이너 안내](https://fastapi.tiangolo.com/deployment/docker/), [FastAPI 메타데이터](https://pypi.org/pypi/fastapi/0.147.0/json), [Uvicorn 메타데이터](https://pypi.org/pypi/uvicorn/0.54.0/json), [HTTPX2 메타데이터](https://pypi.org/pypi/httpx2/2.14.0/json).

`.env.example`을 참고해 MongoDB 자격 증명을 설정한다. 모델은 **API_MODEL_DIR과 API_MODEL_VERSION을 함께** 지정한다. 버전은 그 번들의 manifest.json에 있는 model_version이다. 요청으로 파일 경로를 받거나 디렉터리에서 최신 모델을 찾지 않는다. 모델 미설정 시에도 조회 API는 실행할 수 있지만 /health는 degraded·503, 추천은 model_unavailable·503이다. 경로만 있거나 버전 형식이 틀리면 설정 오류로 시작을 거부한다.

호스트에서 실행할 때 API_MODEL_DIR은 로컬 번들 경로다.

```sh
python -m pip install -r requirements-dev.txt
python -m uvicorn FRS.api.app:app --host 127.0.0.1 --port 8000
```

Compose는 artifacts/models를 컨테이너의 /models에 읽기 전용으로 마운트한다. 이 경우 API_MODEL_DIR은 **/models/명시한실행ID/bundle**이다. 자동으로 기존 2-A/2-B 모델을 선택하지 않는다. 같은 `.env` 경로 값을 호스트/Compose에서 혼용하지 않는다. API 포트는 기본 127.0.0.1:8000이며 Compose의 API_PORT로 바꿀 수 있다. DB 포트는 기존대로 127.0.0.1에만 공개한다.

```sh
docker compose up -d --build
```

이미지는 Python 3.11.15-slim-bookworm의 manifest digest를 고정하고 libgomp1·고정 requirements를 설치한다. 컨테이너는 비루트 사용자로 실행한다. `.dockerignore`의 허용 목록으로 Python 소스·스키마 JSON·requirements만 복사하며 `.env`, Git, 수집 데이터·모델·로컬 캐시는 이미지에 넣지 않는다. 이미지 내 경로를 검사해 이를 확인했다. 인증·외부 공개·배포 구성은 이번 범위가 아니다.

## HTTP 계약

| 경로 | 입력 | 결과 |
|---|---|---|
| GET /health | 없음 | DB ping·시작 시 명시 로드한 모델의 준비 상태. 둘 다 준비됐을 때 200, 미설정/장애는 503 |
| GET /cards/{spid} | 필수 query: batch_id, version | 정확히 해당 관측을 card로 반환. evaluated_at은 서버 UTC 시각, usable은 해당 시각의 관측 신선도 판정 |
| POST /recommend/position | model_version, selection | 기존 ProbabilityResult 계약: model_version·전체 학습 classes·카드별 모든 클래스 확률 |

selection은 기존 InputSelection 구조인 observations(spid·batch_id·version 목록)와 base_reinforce_level이다. 같은 카드를 여러 관측으로 요청할 수 없다. GET의 미지정/추가 query와 POST의 미지정/추가 필드·중복·문자열 카드 ID는 검증 오류다. 조회 시각을 요청으로 과거에 돌려 만료를 우회할 수 없다.

예를 들어 합성 관측은 다음 형태로 조회한다. 실제 DB에 들어 있는 명시 키로 바꿔 사용한다.

```sh
curl --get 'http://127.0.0.1:8000/cards/123000001' \
  --data-urlencode 'batch_id=synthetic-api-batch' \
  --data-urlencode 'version=synthetic-v1'
```

POST 본문 구조 예시이며 model_version은 사용할 번들의 실제 64자리 해시로 바꿔야 한다.

```json
{
  "model_version": "<manifest.json의 model_version>",
  "selection": {
    "observations": [
      {"spid": 123000001, "batch_id": "synthetic-api-batch", "version": "synthetic-v1"}
    ],
    "base_reinforce_level": 5
  }
}
```

MongoCardRepository는 player_card에서 세 키가 정확히 일치하는 문서만 find한다. `_id`만 다른 완전 동일 관측은 중복 제거하고 값이 다르면 409다. find_one·최신 정렬·status 필터로 충돌을 숨기지 않는다. API는 insert/update/delete·컬렉션/인덱스 생성·원본 덮어쓰기를 하지 않는다. 실제 권한은 사용자가 설정한 MongoDB 계정에 달려 있으며 기본 Compose가 자동으로 별도 읽기 계정을 만드는 것은 아니다. 검증에서는 별도 읽기 전용 계정의 쓰기 거부까지 확인했다.

GET은 unknown/만료 문서도 원래 상태·시각·기한 그대로 보여주며 usable=false로 구분한다. usable은 **관측 신선도**이며 모델 필수 피처 충족·가격·팀컬러·스쿼드 추천 적격 보장이 아니다. POST는 요청한 카드 모두에 대해 현재 is_usable_at을 만족해야 추론한다. unknown·stale·미래 관측·만료·삭제 기한 경과는 409이며 과거 학습 허용을 현재 추천 허용으로 확대하지 않는다. 필수 피처·기준 강화·미학습 체형은 기존 공통 계약으로 오류를 반환한다. 가격/급여/팀컬러를 결합하는 스쿼드 추천은 이 경로에 포함하지 않는다.

| 오류 | HTTP | 코드 예시 |
|---|---:|---|
| 없는 명시 관측 | 404 | observation_not_found |
| 충돌·사용 불가 관측·다른 모델 버전 | 409 | observation_conflict, unusable_observation, model_version_mismatch |
| 요청 구조/모델 입력 오류 | 422 | invalid_request, input_contract_error |
| DB 장애·저장 문서 오류·모델 미준비 | 503 | database_unavailable, invalid_stored_data, model_unavailable |

DB/저장 문서의 원본 예외·자격 증명·모델 경로와 검증 요청의 원문 값은 오류 응답에 넣지 않는다. 잘못된 입력의 위치/종류와 필요한 수정 안내만 반환한다. 모델은 프로세스 시작 시 한 번 로드하며 요청 중 재학습·hot reload·다른 번들 탐색을 하지 않는다.

## 검증 결과와 한계

- 기본 pytest의 네트워크 차단 아래 HTTP 테스트 **21개**, 전체 **592개**, Ruff 린트/포맷·환경 import·pip check 통과. 깨끗한 별도 가상환경에서도 고정 requirements 설치 후 592개·Ruff·pip 검사를 통과했다.
- Linux arm64 Docker 이미지 빌드 및 이미지의 로컬 데이터/모델/환경 파일 제외 검사 통과. 기존 2-A 번들은 의존성 추가 후에도 로드됨을 확인했다.
- 별도 무작위 이름의 Compose 프로젝트에서 가상 카드 30개·가상 모델을 생성해 실제 MongoDB·API 컨테이너로 검증했다. health/카드/전체 확률 200, 없는 배치 404, 다른 모델/unknown 관측 409를 확인했다.
- 컨테이너/호스트 확률이 정확히 같고 API 요청 전후 DB 문서가 불변이었다. 테스트 API 계정은 read 권한만 주었고 쓰기 시도는 권한 오류로 거부됐다. 검증 전용 컨테이너·네트워크·볼륨은 정리했고 기존 DB/볼륨은 사용하지 않았다. 합성 모델·보고서는 Git 제외 로컬 artifacts/models 아래에 보관한다.

재현용 선택 명령은 아래와 같다. 이미지 빌드·로컬 Docker·프로젝트 의존성이 필요하며 기본 pytest/CI에서는 실행하지 않는다. 이 명령은 합성 자료만 쓰고 새 넥슨 요청을 하지 않는다. 기존 프로젝트의 `.env` 값을 사용하지 않고 임시 프로젝트·임시 자격 증명을 사용한다.

```sh
docker build -t frs-api:local .
PYTHONPATH=. python scripts/validate_api_compose.py
```

Stage 2-C 골격과 합성 통합 검증 범위를 완료했다. API가 실데이터를 자동 적재하거나 MongoDB 자료의 원본 manifest를 재검증하지는 않는다. 원본 대조를 수행한 적재 경계와 실제 운영 계정/데이터 준비는 별도다. 기존 unknown 과거 자료를 승격하지 않았으므로 학습 성공만으로 실데이터 추천이 가능해진 것은 아니다. Stage 1-D·1-F 전체 미완료, 제공자 허용 미확정, Stage 3 점수/제약 정책 미정은 유지한다. 원격 최신 head CI와 Squash 병합은 PR에서 확인한다.
