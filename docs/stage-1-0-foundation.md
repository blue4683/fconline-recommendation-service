# Stage 1-0 코드 정비 기록

- 날짜: 2026-10-18 (Asia/Seoul)
- 브랜치: `fix/stage-one-foundation`, 최신 `origin/develop`에서 생성
- 경로: 사용자 선택은 실데이터. 이번 변경은 경로와 무관한 공통 정비다.
- 범위: 설계도 C3·C4·C7의 의존성·C8, 검증 환경과 사용자 승인된 기존 코드 기계적 정리

## 사용자 결정

Python 3.11.15 + 저장소 `.venv` + pip, 무작위 User-Agent 제거 및 HTTP 클라이언트 기본 식별자 사용, Python 3.11 호환 안정 버전 선정·고정, 한국어 문서·코드 주석을 적용했다. 기존 파일의 포맷·import 정렬·기본 린트 오류 정리도 별도 질문으로 승인받았다. 모델 학습·평가 알고리즘 변경은 승인 범위에 포함하지 않았다.

Stage 0 전체 종료 후 Stage 1로 가는 설계도 원칙과 달리, `AGENTS.md`가 허용한 독립적인 Stage 1-0 공통 작업을 먼저 진행했다. 실데이터의 지속 수집 허용 범위와 보관 기간을 확정한 것은 아니다. 작업 도중 [Stage 0 문서 PR #8](https://github.com/blue4683/fconline-recommendation-service/pull/8)의 병합을 확인하고 최신 `develop`을 fast-forward로 반영했다.

## 변경 결과

| 항목 | 결과 |
|---|---|
| C3 요청 | 무작위 UA·출처 헤더·잘못된 위치 인자 제거. 클라이언트 기본값 사용. 연결/읽기 타임아웃, 실패 종류·상태 코드 기록 |
| C3 중단 | 병렬 요청을 순차 처리로 전환. 403·429·타임아웃·연결 오류·빈 본문·예상 밖 상태·느린 응답에서 `CollectionStopped`가 상위 루프까지 전파됨 |
| C3 재시도 | 502/503/504만 기본 1회 재시도. 요청 전 1초 간격·2초 backoff. 404는 별도 실패. 기존 바깥 무한 재수집 루프 제거 |
| 요청 설정 | `RequestPolicy`의 시간·간격·재시도 횟수 검증. 기본 총 응답 지연 중단 기준 30초. 기술적 기본값이며 제공자 허용 요청량·D8 정책이 아님 |
| C4 DB | `MONGO_*` 단일 설정, 누락 값·포트 검증, 자격 증명을 URI에 연결하지 않고 드라이버 인자로 전달, 유한한 연결 대기·명시적 ping·close |
| C4 적재 | 생성자 인자 불일치·단독 모듈 import 오류 수정. import 시 접속·파일 적재 제거. 중복 키만 중복으로 처리하고 다른 DB 오류는 전파 |
| C7 의존성 | 누락된 XGBoost·scikit-learn 추가. 불필요한 user_agent 등 제거. 직접·전이 의존성과 개발 도구 버전 고정 |
| C8 Compose | 하드코딩 자격 증명 제거. 필수 환경 변수 검증, localhost 바인딩, 버전·digest 고정. 관리 화면은 tools 프로필에서만 실행하며 Basic Auth 사용 |
| 데이터 보호 | 기존 mongo-data bind mount를 자동 재사용하지 않는 새 named volume. 로컬 데이터·모델·백업·Ruff 캐시 ignore 추가 |
| 실행·검증 | `--check-environment`, pytest 네트워크 차단 fixture, 전체 Ruff 검사, Python 고정 CI 추가 |

기존 파일 정리는 포맷·import 정렬·`None` 비교·불필요한 임시 변수 제거와 bare except를 `except Exception`으로 바꾸는 작업이다. KeyboardInterrupt·SystemExit를 삼키지 않도록 했으며, 모델 분할·스케일링·점수·학습 설정은 정정하지 않았다. 전처리의 import 시 dotenv 로드도 제거하고 DB 설정 생성 시에만 로드한다.

## 선정 버전과 근거

PyPI 릴리스 메타데이터에서 비사전·비철회 릴리스와 Python 3.11 호환 조건을 확인하고 새 `.venv`에 설치했다. 직접 버전은 아래와 같고 전이 의존성은 설치용 고정 파일에 있다.

| 패키지 | 버전 |
|---|---|
| pip | 26.2.1 |
| [beautifulsoup4](https://pypi.org/project/beautifulsoup4/4.15.0/) / [requests](https://pypi.org/project/requests/2.34.2/) | 4.15.0 / 2.34.2 |
| [numpy](https://pypi.org/project/numpy/2.4.6/) / [pandas](https://pypi.org/project/pandas/3.0.6/) | 2.4.6 / 3.0.6 |
| [pymongo](https://pypi.org/project/pymongo/4.19.0/) / [python-dotenv](https://pypi.org/project/python-dotenv/1.2.4/) | 4.19.0 / 1.2.4 |
| [scikit-learn](https://pypi.org/project/scikit-learn/1.9.1/) / [xgboost](https://pypi.org/project/xgboost/3.2.0/) | 1.9.1 / 3.2.0 |
| [tqdm](https://pypi.org/project/tqdm/4.70.1/) | 4.70.1 |
| [pytest](https://pypi.org/project/pytest/9.1.1/) / [Ruff](https://pypi.org/project/ruff/0.17.1/) | 9.1.1 / 0.17.1 |

[PyPI](https://pypi.org/)의 각 패키지 릴리스·`Requires-Python` 및 실제 설치 결과를 기준으로 선택했다. [Requests 공식 문서](https://requests.readthedocs.io/en/latest/user/quickstart/)의 요청 인자·예외, [PyMongo 연결 옵션](https://www.mongodb.com/docs/languages/python/pymongo-driver/current/connect/connection-options/), [Ruff 설정](https://docs.astral.sh/ruff/configuration/), [Compose 필수 변수 문법](https://docs.docker.com/compose/how-tos/environment-variables/variable-interpolation/)을 확인했다.

MongoDB는 기존 7 계열의 안정 태그 `7.0.45-jammy`로 선정했다. [MongoDB 이미지](https://hub.docker.com/_/mongo), [mongo-express 이미지](https://hub.docker.com/_/mongo-express)에서 arm64·amd64 태그와 digest를 확인했다. 관리 화면의 인증 변수는 [해당 버전 설정 소스](https://github.com/mongo-express/mongo-express/blob/v1.0.2/config.default.js)를 대조했다. GitHub Actions의 checkout v7.0.1·setup-python v7.0.0은 공식 저장소 태그가 가리키는 commit SHA로 고정했다.

## 검증 결과

- 새 Python 3.11.15 `.venv`에서 설치 및 `pip check` 통과
- 오프라인 pytest 38개 통과: 차단 시 후속 요청 없음, 제한된 재시도·간격, 잘못된 설정, 중복·일반 오류 구분, 접속 없는 모듈 import 등
- `python -m ruff check .`, `python -m ruff format --check .` 전체 통과
- `python -m FRS --check-environment` 통과
- Compose 설정 검증 및 새 프로젝트명·임의 호스트 포트·새 볼륨으로 실제 컨테이너 실행 통과
- MongoDB 7.0.45의 ping·합성 선수 1건 적재·같은 입력의 중복 처리·특수문자 자격 증명 인증 통과
- mongo-express 비인증 HTTP 401, 임시 계정 인증 HTTP 200 확인
- 통합 검증 후 이번 검증에서 만든 컨테이너·볼륨만 삭제. 기존 DB·볼륨·`.env` 미접근
- 첫 관리 화면 확인은 시작 직후 연결 종료를 만났으며, 검증 절차에 준비 대기를 적용한 재실행에서 통과

CI는 Linux x86_64에서 고정 파일 설치·pip check·pytest·Ruff·환경 실행 확인을 수행한다. 기본 pytest에는 실제 DB나 외부 API 호출이 없다.

## 남은 제한

- 현재 파서 수정·파일럿 수집·스키마·실데이터 적재·모델 학습은 수행하지 않았다.
- D2 보관·삭제, D3 지원 범위, D7 성능 점수, D8 신선도 등은 결정되지 않았다.
- 기존 모델의 평가·누수·출력 계약 문제는 Stage 2에서 처리한다. 의존성 설치·import 성공이 모델 재현 성공을 의미하지 않는다.
- 현재 크롤러의 체크포인트·응답 구조 검증·전체 실패 기록은 Stage 1-B/1-C에서 보완해야 한다. 200 응답의 모든 차단 화면을 현재 공통 요청 함수가 식별하는 것은 아니다.
- 현재 기본 타임아웃과 응답 완료 후 지연 검사는 전체 전송의 엄격한 벽시계 제한을 보장하지 않는다. Requests의 읽기 타임아웃은 소켓 비활성 시간 기준이다.
- mongo-express 1.0.2의 개별 자격 증명 설정은 URI 특수문자 인코딩에 제약이 있다. 관리 화면을 사용할 경우 DB 자격 증명에 URL 안전 문자를 사용한다. Python 클라이언트는 특수문자 인자를 직접 전달한다.
- Windows 환경과 과거 직렬화 모델의 새 버전 호환성은 검증하지 않았다.

다음 단계는 Stage 1-A 스키마이며, 단계 종료 후 다음 진행을 확인하는 설계도 기준을 따른다.
