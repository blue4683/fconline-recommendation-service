# FCOnline 스쿼드 및 선수 추천 서비스 개발 with Python

현재 스쿼드메이커에 있는 AI 추천 기능을 비슷하게 구현하는 프로젝트입니다.

현재는 FRS 2.0의 데이터 스키마·현재 사이트 파서·명시 배치 후보 검색·수동 갱신 도구와 **Stage 2-0 모델 계약, 2-A 재학습, 2-B 평가 정정, 2-C 읽기 전용 API**을 구현했습니다. [MVP 범위·무기한 로컬 보관 정책](docs/mvp-scope.md)에 따라 실제 관측/모델은 Git에서 제외합니다. Stage 1-D 커버리지·팀컬러 발동 규칙, 1-F 실제 갱신과 스쿼드의 실데이터 통합은 미완료입니다. 제공자의 수집 허용 여부도 미확정이며 종료한 수집 승인을 재사용하지 않습니다.

AI Mini-Project의 다음 작업은 [합성 Agent MVP 로드맵](docs/ai-mini-project-roadmap.md)에 정리했습니다. 기존 엔진과 이번 과제의 신규 Agent 층을 구분하고, 로컬 Ollama·검증된 출력·웹/MCP·평가 순으로 진행합니다. [R2 주입 그래프](docs/injected-agent-graph.md) 이후 후보 ID 선택·qwen3:8b 소량 비교, HTTP/SSE·실제 브라우저 연결, 로컬 stdio MCP를 구현·검증했습니다. [W6 평가 실행기](docs/stage-w6-evaluation-implementation.md)의25문장 대역 검증은25/25이며 [실모델 평가](docs/stage-w6-model-evaluation.md)는 전체E2E24/25, 모델 호출 대상근거/의미15/16이었습니다. 준비 차단9개와실모델측정을구분합니다.

[강화 능력치 파생 설계](docs/reinforcement-derivation-contract.md)의 [R0 순수 계산기](docs/reinforcement-derivation-implementation.md)는 1강 값에 누적 보너스를 더해 1~13강 능력치를 계산합니다. MVP 추천 범위는 5~11강을 유지하고 가격·급여 등은 별도 자료로 다룹니다. R1 합성 자료에 연결했고 기존 관측/모델 변환이나 실제 수집은 실행하지 않았습니다.

웹 작업 인계는 [웹 담당 설계도](docs/web-agent-blueprint.md)와 [에이전트 공용 기록](docs/agent-collaboration.md)을 사용합니다. Vue 3·Vite·TypeScript의 화면/통신 클라이언트는 웹 담당이, 엔진·Agent·API와 실제 연결은 통합 담당이 맡습니다. 웹 앱/채팅 API는 미구현이며 백엔드에서는 [R1 합성 자료 공급기](docs/synthetic-agent-material.md)까지 진행했습니다.

[R3 코드 출력](docs/validated-agent-presentation.md)은 검증된 합성 스쿼드 표·정확한 문자열 금액/급여·상태 안내를 만들고 출력 직전 기한을 확인합니다. 현재 설명은 코드 템플릿이며 실제 LLM 서술·웹/API 연결은 후속입니다.

## 제출 자료

- [5분 발표 구성·대본](docs/presentation-5min.md):10월22일13시·PPTX7장, 슬라이드 노트와 로컬 파일 정보

- [AI Mini-Project 결과 보고서](docs/ai-mini-project-results.md):목표·구조·측정 결과·M02 실패·한계
- [재현·시연 안내](docs/demo-reproduction-guide.md):추가 모델 요청 없는 검사/대역 실행, 저장 결과 확인, 모의 화면과 실제 연결의 구분

보고서는 제출용 Markdown 초안입니다. 외부 제출·PPT/녹화·배포는 완료하지 않았으며 실제 모델 호출 승인은 소진됐습니다.

## 포지션 분류 평가

2026-10-19, 1-B/C의 기본 5강 **1,104카드·1,740라벨 행**, 70/30·시드 123·XGBoost 2024 설정·임계값 0.1로 비교했습니다. 포지션 분류 확률은 선수 성능 점수가 아닙니다.

| 분할 / 전처리 fit | 겹친 카드/인물 | 보류 행 라벨 포함률 | 검증 카드 전체 등록 라벨 포함률 | 카드 하나 이상 포함률 | 평균 예측 집합 크기 |
|---|---:|---:|---:|---:|---:|
| 기존 행 분할 / 전체 | 240 / 261 | 43.68% | 64.15% | 96.54% | 1.5411 |
| 카드 그룹 / 전체 | 0 / 84 | 69.40% | 69.40% | 92.17% | 2.0602 |
| 카드 그룹 / train | 0 / 84 | 69.40% | 69.40% | 92.17% | 2.0602 |
| 인물 그룹 / 전체 | 0 / 0 | 66.12% | 66.12% | 87.74% | 2.0710 |
| 인물 그룹 / train | 0 / 0 | 66.12% | 66.12% | 87.74% | 2.0710 |

행 분할의 카드 지표는 train에 다른 등록 라벨이 남은 카드도 포함합니다. 보류 행 지표와 분모가 다릅니다. 같은 그룹 분할에서 전체/train fit의 확률은 이번 실행에서 동일했지만 정상 모델은 train에만 fit합니다. 검증 표본이 다른 분할 사이의 점수 차이를 누수의 순수 효과로 단정하지 않습니다. 포지션별 재현율·표본 수·비교용 모델 격리·재현 명령은 [2-B 평가 기록](docs/stage-2-b-evaluation.md), 기본 모델은 [2-A 기록](docs/stage-2-a-baseline.md)을 확인하세요. API는 명시한 번들만 로드하며 실데이터 자동 적재·새 수집은 실행하지 않았습니다.

성능 점수는 별도의 [3-A 성능 계약 코드](docs/stage-3-a-performance-implementation.md)에서 포지션별 OVR÷2로 계산합니다. [등록 계열 기본 배치 계약](docs/stage-3-a-placement-implementation.md)도 합성 검증으로 구현했고 모델 확장은 꺼두었습니다. [요청별 순수 스냅샷](docs/stage-3-0-snapshot-implementation.md)은 후보 검색·성능·기본 배치를 결합하며 허용 강화 집합과 전체 만료 정책을 합성 자료로 검증했습니다. [CP-SAT·탐욕 비교·독립 검증기](docs/stage-3-b-c-engine-implementation.md)도 합성 자료로 구현했습니다. 동일 인물 최대 1명·전원 클럽 자격·정확한 BP/급여 합계와 응답 만료를 검증하며, [합성60문제 벤치마크](docs/stage-3-e-benchmark-results.md)의 D6 기능 기준도 통과했습니다. 합성 지연 p95≤1초 기준도 통과했으며 DB/API 연결·실데이터·운영 성능은 후속입니다.

Stage 4-A의 [원문 구간·정확 금액 검증기](docs/stage-4-a-amount-implementation.md)와 [필수 5조건 정규화](docs/stage-4-a-conditions-implementation.md)를 구현했습니다. 수동/합성 추출 근거로 예산·급여·정식 클럽/포메이션·강화 집합을 검증하며 누락·충돌·미지원·오류가 있으면 완성 조건을 반환하지 않습니다. 새 합성120개 포함 전체1,051개 테스트가 통과했습니다. 원문 전체의 의미 추출·LLM·DB/API·스쿼드 연결은 후속입니다.

[4-B 합성 연결](docs/stage-4-b-build-implementation.md)은 수동/합성 근거를 정규화한 뒤 명시 배치·시각의 메모리 스냅샷과 기존 CP-SAT·독립 검증기를 호출합니다. 정규화에 문제가 있으면 자료·엔진을 사용하지 않습니다. 새 연결46개 포함 전체1,097개 테스트가 통과했으며 실제 LLM 추출·DB/API·실데이터 실행은 후속입니다.

## 합성 스쿼드 벤치마크

2026-10-20, 포메이션3종·후보3/10/30·생성시드123·CP-SAT10초/1스레드/시드123으로 측정했습니다. 해 존재45개(정규30+작은15), 해 없음15개입니다.

| 방식/검증 | 결과 |
|---|---:|
| CP-SAT 유효해 반환 | 45/45, 전부 optimal |
| 최저가 / 성능 탐욕 유효해 | 35/45 / 27/45 |
| 반환해 제약 / 작은 최적값 / 상태 | 107/107 / 15/15 / 180/180 |
| OPTIMAL의 유효 탐욕 대비 | 개선45·동점17·악화0 (62회) |
| CP-SAT 스냅샷 포함 p50/p95 | 24.2/194.0ms |

stage-3-e-20261020-003에서 기능 기준과 승인한 합성 지연 p95≤1초를 통과했습니다. 지연은 로컬 macOS arm64 합성 관측이고 DB/HTTP·프로세스 시작은 포함하지 않습니다. 가상 원본 생성/전수 탐색도 별도 평가 비용입니다. 재현은 `python -m FRS.core.squad_recommend.benchmark --run --run-id <new-run-id>`이며 [전체 결과·범위·보존 실행](docs/stage-3-e-benchmark-results.md)을 확인하세요.

## 개발 환경

저장소 루트에서 Python **3.11.15**를 사용합니다. 문서·코드 주석은 한국어로 작성합니다.

```sh
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install pip==26.2.1
python -m pip install -r requirements-dev.txt
python -m pip check
python -m FRS --check-environment
```

`--check-environment`는 모듈 import만 확인하며 수집·DB 접속·학습을 실행하지 않습니다. macOS arm64에서 검증했고 Linux x86_64는 GitHub Actions에서 같은 Python 버전으로 검증합니다. XGBoost import 시 macOS에 OpenMP 런타임이 없으면 [공식 설치 안내](https://xgboost.readthedocs.io/en/stable/install.html)를 확인하세요. Windows 환경과 기존 모델 파일의 새 의존성 호환성은 검증하지 않았습니다.

- 직접 의존성: `requirements.in`, `requirements-dev.in`
- 설치용 고정 파일: `requirements.txt`, `requirements-dev.txt` (전이 의존성 포함)
- Ruff·pytest 설정: `pyproject.toml`
- CI: `.github/workflows/ci.yml`

의존성 갱신은 `.in`의 직접 버전을 바꾸고 별도 깨끗한 가상환경에 `python -m pip install -r requirements-dev.in`으로 설치한 후, `pip freeze`와 패키지 의존성 메타데이터를 대조해 두 설치용 고정 파일을 갱신합니다. macOS의 freeze에 없는 Linux 전용 `nvidia-nccl-cu12` 조건부 고정도 유지·검증해야 합니다. 고정 파일 설치, `pip check`, 아래 검증까지 수행한 변경만 반영합니다. FastAPI·Uvicorn·개발 테스트용 HTTPX2와 LangGraph 1.2.16/LangSmith SDK 0.14.11을 고정했습니다. 외부 추적 전송은 비활성화합니다.

## 검증

```sh
python -m pytest
python -m ruff check .
python -m ruff format --check .
```

기본 테스트는 네트워크를 금지하고 합성 데이터와 목을 사용합니다. Ruff는 기본 오류·미사용 코드·import 정렬 규칙 `E4/E7/E9/F/I`와 88자 포맷을 사용합니다.

## 로컬 DB 설정

```sh
cp .env.example .env
```

`.env`에 `MONGO_USERNAME`, `MONGO_PASSWORD`, `MONGO_EXPRESS_USERNAME`, `MONGO_EXPRESS_PASSWORD`를 직접 채운 후 실행합니다. Compose는 비활성 프로필의 변수도 해석하므로 네 값을 모두 설정해야 합니다. 예제에는 자격 증명 기본값이 없으며 `.env`는 커밋하지 않습니다. 기존 `HOST/PORT/USERID/PASSWORD` 설정은 새 `MONGO_*` 이름으로 옮겨야 합니다.

```sh
docker compose up -d mongodb
```

DB는 `127.0.0.1:27017`에 열리며 `MONGO_PORT`로 호스트 포트를 변경할 수 있습니다. Python은 환경 변수 또는 저장소 루트 `.env`를 사용하고 기존 환경 변수를 우선합니다. `Client()`는 연결을 지연하며 `ping()`으로 실제 연결을 확인합니다.

```sh
python -c 'from FRS.utils.db.client import Client; c = Client(); print(c.ping()); c.close()'
```

선택 관리 화면은 아래 명령으로 실행하며 기본 주소는 `http://127.0.0.1:8081`입니다. `MONGO_EXPRESS_PORT`로 포트를 바꿀 수 있습니다. mongo-express 1.0.2의 개별 자격 증명 설정은 URI를 직접 조립하므로, 이 관리 화면을 쓸 때 DB 사용자명·비밀번호는 영문·숫자로 구성한 충분히 긴 값을 사용하세요. Python DB 클라이언트의 특수문자 자격 증명은 별도로 검증했습니다.

```sh
docker compose --profile tools up -d
docker compose --profile tools down
```

이미지는 MongoDB `7.0.45-jammy`, mongo-express `1.0.2-20-alpine3.19`의 버전과 digest를 고정했습니다. 기존 MongoDB 7 계열을 유지하며 새 named volume `frs-mongo-data`를 사용합니다. 이전 `./mongo-data`는 자동으로 연결·이관·삭제하지 않습니다. DB 초기화 계정은 새 볼륨 생성 시 설정되므로 `.env` 변경만으로 기존 계정이 바뀌지는 않습니다.

현재 데이터 적재 CLI는 기존 파일 형식 호환용이며 import 시 적재하지 않습니다. 실제 수집·적재·학습은 데이터 정책과 해당 단계의 선행 조건 확인 후 진행합니다. [Stage 1-0 구현 기록](docs/stage-1-0-foundation.md)에 검증 범위와 남은 제한을 기록했습니다.

기존 관측에서 입력 생성·학습·매니페스트 저장/로드까지의 명령과 실제 산출물 재생 결과는 [2-D 재현성 기록](docs/stage-2-d-reproducibility.md)을 따릅니다. 수집 데이터와 모델이 없는 새 환경에서는 합성 검증만 재현할 수 있습니다.

## 로컬 API

`.env`의 API_MODEL_DIR과 API_MODEL_VERSION에 사용할 번들 경로·매니페스트 버전을 직접 지정합니다. Compose 경로는 /models/실행ID/bundle이며 기본 API 주소는 127.0.0.1:8000입니다. 미설정이면 /health가 503을 반환하며 모델을 자동 선택하거나 학습하지 않습니다.

```sh
docker compose up -d --build
```

GET /health, GET /cards/{spid}(필수 batch_id·version), POST /recommend/position(명시 model_version·selection)을 제공합니다. MongoDB는 조회만 하며 unknown/만료 관측은 현재 추천에서 거부합니다. 실제 데이터 적재는 수행하지 않았습니다. 요청/오류 스키마, 호스트 실행·계정 권한·합성 Compose 검증은 [2-C API 기록](docs/stage-2-c-api.md)을 확인하세요.

## Skills

### Back

<img src="https://img.shields.io/badge/Python-3776AB?style=for-the-badge&logo=python&logoColor=white">

### DB

<img src="https://img.shields.io/badge/MongoDB-47A248?style=for-the-badge&logo=mongodb&logoColor=white">
<img src="https://img.shields.io/badge/Docker-2496ED?style=for-the-badge&logo=docker&logoColor=white">

로컬 추출 어댑터와 1회 비교의 범위·사전 점검 중단 결과: [Ollama 비교 기록](docs/ollama-extraction-comparison.md). 새 승인으로 두 모델×5회 비교를 완료했으며 현재 직접 구간 생성 방식은 두 모델 모두 의미 0/5로 채택 보류다.

정형 조건문의 후보 생성·ID 검증·기존 엔진 연결(합성 검증): [C1~C3 구현 결과](docs/controlled-candidate-selection.md). [실제 ID 선택 소량 비교](docs/ollama-candidate-selection-results.md)에서는 8B 6/6·2B 5/6의 근거/정규화 일치를 확인했습니다. 정형 입력의 제한된 검증이며 사용자는 qwen3:8b를 데모 추출 모델로 채택했습니다. [HTTP/SSE 설계](docs/agent-http-sse-contract.md) 뒤 [Agent API](docs/synthetic-agent-api.md)와 [실제 브라우저3사례](docs/agent-browser-e2e.md)를 구현·검증했습니다. 아래 초기 단계와 후속 결과를 구분합니다.

[W4-1 웹 경계 코드](docs/agent-web-contract-implementation.md)는 엄격 DTO·오류 투영·요청/SSE 직렬화를 합성 검증했습니다. 후속 W4-2/3에서 실제 Agent HTTP 앱과 브라우저 연결을 별도로 검증했습니다.

[별도 합성 Agent HTTP/SSE 앱](docs/synthetic-agent-api.md)은 GET /health·POST /recommend/squad/stream과 단일실행/기한/중단을 합성 검증했습니다. 수동 실행은 명시한 합성 배치/관측 시각·모델 digest와 실제 모델 상태 확인 범위가 필요합니다. 실제 모델 ASGI/브라우저3사례 검증을 마쳤고 검증용 서버는 종료했습니다. W6의 내부25문장 평가와 범위를 구분합니다.


### 로컬 stdio MCP (합성 데모)

원문과 후보 ID 선택을 기존 검증/엔진에 연결하는 `build_squad` Tool, 지원조건 Resource, ID 선택 Prompt를 제공한다. 모델 호출·호스트 자동 등록·HTTP MCP는 없다. 입력/실행 정책과 명시 배치/시각 실행 명령·고정 합성 대조는 [MCP 구현 기록](docs/stage-w5-mcp-implementation.md)을 따른다. 웹은 기존 HTTP 경로를 계속 사용한다.


### W6 로컬 평가 (실제 모델 호출 없음)

[사전 정답25문장](docs/stage-w6-evaluation-cases.md)을 정답 선택 대역으로 실행해 조건·차단·기존 엔진/출력을 독립 채점합니다. 실제 모델 정확도와 구분하며, [구현 기록](docs/stage-w6-evaluation-implementation.md)에 초기 실패/최종 실행·해시·지표 분모·한계를 남겼습니다. 최종 합성 대역 결과는25/25, 신규61/전체pytest1,553개 통과입니다.

```sh
.venv/bin/python -m FRS.evaluation.offline \
  --run-id w6-offline-example-new \
  --observed-at 2026-10-21T00:00:00+00:00 --run
```

고정 과거 합성 시계의 예시입니다. `--run`을 빼면 계획만 출력합니다. 실행 ID는 새로 지정하고 결과는 Git 제외 `artifacts/agent-evaluations/<run-id>/`에서 확인합니다. 기존 결과는 덮어쓰지 않습니다. 실제 모델 평가·새 호출·외부 업로드는 이 명령에 포함되지 않습니다.


### W6 실제 모델 평가 결과

고정25문장에로컬qwen3:8b를새1회적용했습니다. 준비차단9개는미호출하고상태2회·추론16회를사용했습니다. **전체E2E24/25(96%), 모델대상근거/의미15/16(93.75%), 정상유효11명8/8**입니다. 예산이빠진M02에서모델의잘못된필드배정을안전하게막았으나기대명확화에실패한것으로집계했습니다.

[실행기·측정·실패·한계](docs/stage-w6-model-evaluation.md)에지연/토큰·실행소스/해시·새승인소진·서버종료를기록했습니다. 작은공개정형세트1회의결과이며일반한국어이해/운영SLA/실제선수추천품질을뜻하지않습니다. 기존승인은소진됐고실행을자동으로반복하지않습니다.
