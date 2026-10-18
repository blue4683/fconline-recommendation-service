# FCOnline 스쿼드 및 선수 추천 서비스 개발 with Python

현재 스쿼드메이커에 있는 AI 추천 기능을 비슷하게 구현하는 프로젝트입니다.

현재는 FRS 2.0의 **Stage 1-A 데이터 스키마**까지 구현했습니다. [새 스키마 계약](docs/stage-1-a-schema.md)과 [MVP 범위·무기한 로컬 보관 정책](docs/mvp-scope.md)을 반영했습니다. 실데이터 적재·학습·새 추천 엔진은 후속 단계입니다. 기존 파서는 현재 사이트에 맞지 않으며 기존 학습·평가의 알려진 문제도 아직 남아 있습니다.

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

의존성 갱신은 `.in`의 직접 버전을 바꾸고 별도 깨끗한 가상환경에 `python -m pip install -r requirements-dev.in`으로 설치한 후, `pip freeze`와 패키지 의존성 메타데이터를 대조해 두 설치용 고정 파일을 갱신합니다. macOS의 freeze에 없는 Linux 전용 `nvidia-nccl-cu12` 조건부 고정도 유지·검증해야 합니다. 고정 파일 설치, `pip check`, 아래 검증까지 수행한 변경만 반영합니다. 미래 단계의 FastAPI·LangGraph는 아직 추가하지 않았습니다.

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

## Skills

### Back

<img src="https://img.shields.io/badge/Python-3776AB?style=for-the-badge&logo=python&logoColor=white">

### DB

<img src="https://img.shields.io/badge/MongoDB-47A248?style=for-the-badge&logo=mongodb&logoColor=white">
<img src="https://img.shields.io/badge/Docker-2496ED?style=for-the-badge&logo=docker&logoColor=white">
