# Stage 2-D 기존 관측에서 모델까지의 재현성

2026-10-19. 기존 2-A/2-B 승인 범위의 데이터 생성·학습·저장 스크립트와 실제 산출물을 정리한다. 이번 점검은 **읽기 전용**이며 새 수집·실데이터 학습·관측 상태 승격을 실행하지 않았다. 재현 대상은 2026년에 저장한 1-B/C 자료와 현재 고정 런타임이다. 2024년의 원본 데이터·모델 바이트를 복원한 뜻은 아니다.

## 스크립트화한 경로

| 순서 | 구현 | 고정/검증 내용 |
|---|---|---|
| 완료 배치의 원본 대조 | diagnostics.model_input_report.load_verified_cards | 명시 배치·완료 상태·기존 원본/가공 검증기·파일 집합 해시 |
| 입력 데이터 생성 | data_contract.to_labeled_input | 기본 5강·40피처·등록 라벨 행·명시 관측 키·동일 카드 그룹 |
| 분할·전처리·학습 | position_recommend.train / compare | 70/30·시드 123·고정 XGBoost 설정, 정상 모델은 그룹 분리/train fit |
| 모델·변환·매니페스트 저장 | artifacts.save_bundle | 새 디렉터리, feature spec·출처·분할·런타임·파일 해시·model_version |
| 명시 버전 로드·확률 재현 | artifacts.load_bundle / PositionPredictor | 같은 변환/인코더, 전체 클래스, 기존 검증 확률과 정확 일치 |

여기서 데이터 생성은 저장한 검증 관측을 학습 행으로 변환하는 작업이다. 원본 웹 페이지를 다시 수집하거나 완료된 수집 실행을 재시작하지 않는다. train.py가 위 경로를 연결하고 run.json에 진행/완료/중단, report.json·검증 정답/확률에 결과를 남긴다. compare.py는 5가지 비교와 동일 검증을 수행한다. 누수가 있는 전체 fit 결과는 comparison_only에 격리하며 정상 추론 번들로 사용하지 않는다.

## 준비와 실행

Python 3.11.15·requirements-dev.txt의 정확한 버전을 사용한다. 원본/가공은 Git에 없으므로 동일하게 보관한 로컬 1-B/C 자료가 필요하다. 저장소만 복제한 환경에서 실데이터 점수까지 재현된다고 보고하지 않는다. 기본 pytest는 이 자료 없이 합성 계약을 검증한다.

원본 대조와 입력 생성만 확인하는 명령은 모델을 학습하거나 DB/넥슨에 접속하지 않는다.

```sh
python -m FRS.diagnostics.model_input_report \
  --batch-id stage-1-b-20261018 stage-1-c-20261018
```

아래 기본 명령은 각각 계획만 출력한다.

```sh
python -m FRS.core.position_recommend.train
python -m FRS.core.position_recommend.compare
```

실제 재학습이 필요하면 사용자가 새 로컬 실행 ID와 범위를 명시한다. 다음은 형식 예시이며 이번 점검에서는 실행하지 않았다. 기존 완료/중단 디렉터리는 재사용하지 않는다. 로컬 재학습을 추가 외부 수집 승인으로 해석하지 않는다.

```sh
python -m FRS.core.position_recommend.train \
  --run --run-id stage-2-a-reproduce-example-001
python -m FRS.core.position_recommend.compare \
  --run --run-id stage-2-b-reproduce-example-001
```

세부 설정·지표 정의·출처 해시·저장 구조는 [2-A 학습](stage-2-a-baseline.md), [2-B 비교](stage-2-b-evaluation.md), [공통 산출물 계약](stage-2-0-artifact-contract.md)을 따른다. API/컨테이너의 합성 재현은 [2-C](stage-2-c-api.md)의 별도 선택 검증 스크립트를 사용한다. 모델 버전을 바꾸어 비교 실패를 숨기거나 최신 관측·임의 시드·추가 자료로 보정하지 않는다.

## 실제 읽기 전용 점검

소켓 connect/connect_ex 및 requests 전송을 차단하고 완료된 1-B/C 원본을 다시 대조했다. 1,104카드의 입력 해시는 **45cc940281f63139063344dc07b8a58e093d5e4c2c344f38dd2ee3f089aa628d**로 2-A/2-B 기록과 같다. 두 실행의 completed 상태·출력 파일 해시·보고서의 출처 해시·번들의 학습 context가 현재 검증 입력과 일치했다.

| 실행 / 번들 | 검증 카드 | 명시 model_version | 기존 저장 확률 재현 |
|---|---:|---|---|
| stage-2-a-20261019-001 / bundle | 332 | 4c4e1562cbb181ab99e45e6d1d6ab14c8a6eda3886682c8878694502db2e3be2 | 정확 일치 |
| stage-2-b-20261019-001 / card_train/bundle | 332 | 4c4e1562cbb181ab99e45e6d1d6ab14c8a6eda3886682c8878694502db2e3be2 | 정확 일치 |
| stage-2-b-20261019-001 / person_train/bundle | 310 | 86b607c28fcf2a0da1fe04a06533d651e086a84ed69b101fb132e99ea91c89be | 정확 일치 |

각 번들에 기록한 test_indices/row_spids에서 검증 카드를 복원하고 원래 관측 선택·강화로 predict_proba를 호출했다. JSON 왕복한 전체 클래스/카드별 확률이 기존 validation_probabilities.json과 같았다. 점검 요약은 Git 제외 artifacts/models/stage-2-d-reproducibility-verification.json에 보관했다. 새 HTTP 0회·새 학습 0회이며 기존 산출물을 덮어쓰지 않았다.

## 완료 범위와 한계

기존 스크립트로 저장 관측→학습 입력→학습→매니페스트 저장/로드 경로를 재현할 수 있고, 현재 로컬 자료의 기존 결과 재생을 확인했다. 이 범위에서 2-D 재현성 기록을 완료한다. 사용자 D2에 따라 데이터·모델·개별 평가 파일은 Git 제외 로컬에 무기한 보관하며 생성 스크립트와 계약만 버전 관리한다.

런타임·자료·특징 스펙·모델 버전이 다르면 같은 실험이 아니다. 다른 하드웨어/라이브러리에서 학습 바이트나 확률까지 같다는 보장은 검증하지 않았다. 제공자 허용, 실데이터 적재/운영 준비, Stage 1-D·1-F와 Stage 3의 제품 정책은 별도이며 이번 재현성 확인으로 완료·승격하지 않는다.
