# 웹 자동 검증 CI

2026-10-22. 사용자가 MF-2 실제 평가 준비를 다른 담당에게 맡겼음을 확인하고, 병행 가능한 웹 CI를 구현했다. 기준은 develop `b568efbacc50a8d1c4bb2f541c4de4e5aca272a4`와 당시 [공용 기록](agent-collaboration.md)·[웹 설계도](web-agent-blueprint.md)다. Stage 1-0 검증 기반/Stage 4 웹·INT-004 후속이며 D1 실데이터 경로와 기본 합성 실행은 유지한다.

## 변경 결과

기존 [.github/workflows/ci.yml](../.github/workflows/ci.yml)은 Python만 검사했다. 새 [frontend.yml](../.github/workflows/frontend.yml)은 같은 세 대상 develop·master·feat/real-data의 pull_request/push에서 `frontend` job을 실행한다. 경로 필터를 두지 않아 백엔드나 계약 문서만 바뀐 PR에서도 웹 검사를 수행한다. 기존 Python workflow·`quality` job과 검사 기준은 수정하지 않는다.

| 항목 | 구성 |
|---|---|
| 실행 환경 | ubuntu-24.04·최대10분 |
| 도구 버전 | 기존 웹 로컬 검증과 같은 Node22.23.3·npm10.9.9 |
| Action | 기존 checkout v7.0.1 SHA와 setup-node v6.5.0 SHA 고정 |
| 권한 | contents:read |
| 의존성 | frontend/package-lock.json 기준 `npm ci --no-audit --no-fund`. npm 다운로드 캐시의 키도 이 lock으로 고정 |
| 명령 위치 | frontend/ |
| 검사 | typecheck → test → build. 실패 무시·조건부 성공 처리 없음 |

setup-node의 고정 버전/캐시 입력은 [공식 소스](https://github.com/actions/setup-node/tree/249970729cb0ef3589644e2896645e5dc5ba9c38)와 action.yml을 확인했다. node/npm 버전을 로그로 남긴다. 패키지 버전·lock·웹 앱·원문/스쿼드/점수/HTTP 계약은 바꾸지 않았다.

## 검증

- 로컬 Node22.23.3·npm10.9.9에서 깨끗한 작업 checkout의 npm ci·typecheck·Vitest **6파일/129테스트**·Vite 프로덕션 빌드 통과.
- Python3.11.15에서 전체 pytest **2,089개 통과(35.17초/4worker)**. 최종 문서 작성 후 전체 Ruff lint/format(278파일)·환경 import·pip check·새 로컬 링크 검사를 통과했다.
- YAML 파싱, Python workflow와 동일한 이벤트/대상, 작업 디렉터리·cache lock 경로·Action40자리 SHA·실패 무시 없음과 Git diff를 검토했다.
- GitHub의 실제 신규 `frontend` job과 기존 `quality` job은 최신 PR head에서 모두 확인한 뒤 병합한다. 로컬 검증을 원격 CI 성공으로 보고하지 않는다.

## 실행 범위와 제한

웹 검사는 기존 가상 fixture·mock transport와 저장된 합성 백엔드 응답 대조를 사용한다. 실행 중인 Vite/Agent/Ollama·DB·실제 관측 자료에 연결하지 않으며, 모델/넥슨/metadata/추적 요청이나 실제 브라우저 E2E는 실행하지 않는다. 의존성 설치/캐시와 GitHub CI 자체의 통신은 이 제한과 구별한다.

이 workflow는 최신 백엔드로 fixture를 매번 재생성하거나 배포하지 않는다. 브랜치 보호의 required check 등록, 웹 담당의 경로 권한 자동 차단/파일 격리도 구현하지 않았다. 이들 항목과 MF-2 판단 수집/평가·MF-3 연결·master PR#143의 충돌 해결/병합은 별도 범위다. 소진 승인·관측 기한·1-D/1-F 미완료와 타 담당 작업을 보존한다.
