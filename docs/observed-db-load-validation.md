# 기존 관측 자료의 로컬 MongoDB 적재·대조

실데이터 설계 RD-3·단계4(DB 사본)의 실제 검증이다. 기준 코드는 `feat/real-data`의 `e0cf3a6`이며, 실행은 2026-10-21 23:49:53~23:49:56 KST에 `completed`로 종료했다. 실행 ID는 `observed-db-load-20261021-001`, 자료 ID는 별도로 `real-data-20261021-b`를 유지한다.

## 사용자 승인과 환경

사용자가 다음 단계 진행을 요청한 뒤 **전용 계정 자동 생성·로컬 .env 저장 후 이미지 설치와 적재 진행**을 선택했다. 주 체크아웃의 `.env`에 비어 있던 MongoDB 설정을 추가했고, 기존 파일 바이트와 비어 있지 않은 설정값은 보존했다. Compose가 비활성 관리 화면의 변수도 해석하므로 `MONGO_USERNAME`·`MONGO_PASSWORD`·`MONGO_EXPRESS_USERNAME`·`MONGO_EXPRESS_PASSWORD` 네 필수 값을 채웠다. 비밀값은 `.env`에만 저장하고 권한을0600으로 설정했다. 코드·문서·실행 기록·출력에는 값을 넣지 않았다.

이 프로젝트의 MongoDB 컨테이너·볼륨과27017 포트 점유가 없음을 먼저 확인했다. 저장소의 고정 이미지 `mongo:7.0.45-jammy@sha256:7fba8c37d681a79f22a85d2b06bd5ed121a7973c0a6ca20ed4887bb0a1548ce0`를 받아 MongoDB 서비스만 실행했다. 실제 buildInfo는7.0.45이며 인증된 접속 대상은 **127.0.0.1:27017/frs**다. 기존 다른 프로젝트의 컨테이너와 데이터는 수정하지 않았다.

```sh
docker compose --project-directory /path/to/main-checkout \
  --env-file /path/to/main-checkout/.env -p fconline-recommendation-service \
  up -d --no-deps --no-build --pull never mongodb
```

서비스는 `fconline-recommendation-service-mongodb-1`, 영속 볼륨은 `fconline-recommendation-service_frs-mongo-data`다. 적재 후 MongoDB를 유지한다. 기존5173의 합성v0(PID31483)는 그대로이며 모델·Agent 서버를 새로 실행하지 않았다.

## 기준 자료와 적재 전 확인

자료 담당 체크아웃의 `data/processed/real-data-20261021-b`를 직접 읽었다. 파일을 다른 체크아웃으로 복사하거나 다시 수집하지 않았다. `report.json` SHA256은 `53787a5cd3560f3960a5808a08abdba9f704024e9c7c7c7d13426db23dbbaf71`, material ID는 `08a698c865b1030a87568cfa02cea8ab5ed838bea9b7cd2b2176faeeab9fa31f`다. 보고서의 가공5파일 해시와 기존 E2E 기록을 유지했다.

사전 검사에서 대상5컬렉션과 `observed_load`가 모두0건이고 DB에 기존 컬렉션이 없음을 확인했다. 기존 모델의 BSON 인코딩/복원으로35,948건 모두 파일의 typed model과 같았다. 이후 소진 잠금·요청 전 기록을 만들고 기존 `load_observed_run()`을 한 번 적재했다. 재시작/자동 재적재는 하지 않았다.

## 실제 대조 결과

| 컬렉션 | 적재·전량 대조 | 상태 |
|---|---:|---|
| player_card | 1,634 | ok1,634 |
| card_salary | 11,438 | ok11,438 |
| card_price | 11,438 | ok11,347·unknown91 |
| reinforce_stats | 9,804 | ok9,804 |
| team_color | 1,634 | ok1,634 |
| 합계 | **35,948** | 원래 상태 유지 |
| observed_load | 1 | 명시 run-id/material ID/가공해시·적재시각 기록 |

각 컬렉션의 모든 문서를 기존 스키마로 복원한 뒤, 순서와 Mongo `_id`를 제외한 정규화 행 목록을 원본 공급기의 모든 행과 대조했다. 관측값·강화·출처·배치·원래 시각·기한·unknown 상태가 전부 일치한다. 모델 행의 정규 SHA256과 상태별 건수도 실행 manifest에 남겼다. `candidate_lookup` 인덱스5개가 기존 검색 계약과 일치하며 유일성/신선도 필터를 임의로 추가하지 않았다.

기존 `MongoCandidateRepository.search()`와 파일 기반 순수 검색도 같은 기준시각·배치·리버풀·정확한 등록ST·급여상한99·가격상한130,000 BP에서 전체 결과가 같았다. 5강은265후보, 11강은0후보였다. 이0은 해당 조건의 결과이며 11강 관측 자료 부재를 뜻하지 않는다.

동일 배치 재적재의 거부 경로는 인덱스/쓰기 진입을 차단한 읽기 전용 probe로 확인했다. `LoadRefused`가 발생했고 컬렉션 건수와 적재 기록1건이 유지됐다. 실제 데이터 적재는1회다. 실패 주입에 의한 롤백을 실데이터 DB에서 실행하지는 않았다(기존 합성 단위 테스트의 범위).

가격의 원래 가장 이른 기한 **2026-10-22 17:03:29.357 KST**는 그대로다. 새 `loaded_at`은 DB 사본의 적재 시각이며 관측 시각·유효 기한을 바꾸지 않는다. 만료 후 DB에 남아 있는 자료를 저장 이유만으로 추천에 다시 사용할 수 없다.

## 재확인과 보존

원본 기준은 기존 로컬 파일이며 MongoDB는 다시 만들 수 있는 사본이다. 같은 run-id의 `--write` 재실행은 기존 자료를 덮어쓰지 않고 거부한다. 새 환경에서는 아래 기본 CLI로 **적재 계획만** 확인한다. DB 쓰기는 `--write`와 명시한 로컬 DB 설정이 필요하다.

```sh
python -m FRS.utils.db.load_observed \
  --root /path/to/data-checkout --run-id real-data-20261021-b
```

현재 환경의 읽기 전용 건수·감사 기록 확인기는 주 체크아웃의 `artifacts/handoff/observed-db-readonly-check-20261021.py`다. 기존 Python 환경에서 실행하며 `.env`를 내부에서 읽고 비밀값은 출력하지 않는다.

```sh
/path/to/python artifacts/handoff/observed-db-readonly-check-20261021.py
```

주 체크아웃의 Git 제외 `artifacts/observed-db-load/observed-db-load-20261021-001/manifest.json`에 적재1회·전량 대조·인덱스·검색·재적재 거부 결과를 보존한다. completed manifest SHA256은 `7193899fafb680981c1168ab202203833d83469b6e4b29b87ce0438e35ba2fa9`다. 사전 계획/인증 환경(비밀값 제외)/BSON 검증은 `artifacts/handoff/observed-db-*-20261021.json`에 보존했다. DB 덤프·관측 행·비밀값은 커밋하지 않는다.

이번 증분은 기존 적재기의 실제 로컬 검증과 문서 정리다. 새 수집·Ollama metadata/추론·LangSmith 전송0회이며 E2E003의 소진 승인을 재사용하지 않았다. Agent의 관측 자료 공급은 기존 명시 파일 경로를 유지한다. API/웹의 DB 공급기로 교체하거나 실제 모델 API의 DB 추천을 검증한 작업은 아니다. master/develop·다른 담당 체크아웃/브랜치·기존 종료 기록과 사용자 파일은 보존했다.

기능 코드를 변경하지 않아 로컬 전체 pytest/Ruff/웹 검사를 재실행하지 않았다. 문서 내용·링크·diff와 실제 전량 대조/증거 해시를 검토하고, PR의 최신 head CI는 별도로 확인한다. 전체1-D/1-F·클럽/강화/포메이션 커버리지나 운영SLA 완료로 확대하지 않는다.
