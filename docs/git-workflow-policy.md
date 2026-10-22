# 완료 브랜치 정리와 기능 묶음 PR

2026-10-20 사용자 확정: 기존 완료분 포함 로컬·원격 삭제, 동작 가능한 기능 묶음의 구현·검증 완료 시 `develop → master` PR 생성. master 병합과 배포는 별도 요청이다.

## 판정과 보존

PR이 실제 병합됐고 현재 브랜치 끝이 PR head와 일치하며 병합 결과가 최신 develop에 포함됐는지 대조한다. 브랜치 끝이 PR의 최종 병합 커밋 자체라면, 그 커밋의 develop 도달성·추가 커밋/활성 작업 부재로 완료를 확인할 수 있다. 다른 열린 PR과 사용 중인 작업 디렉터리도 확인한다. Squash 병합은 원래 head가 develop의 조상이 아닐 수 있으므로 `branch --merged`만 사용하지 않는다. 삭제 직전에 원격/로컬 끝이 바뀌지 않았는지 확인한다. 작업 중인 브랜치는 먼저 이동한다. 강제 푸시·기존 이력 재작성은 하지 않는다.

보호 브랜치 develop/master/main, 미병합·추가 변경·다른 담당의 활성 브랜치는 보존한다. 자동 정리할 근거가 부족하면 보류하며 사용자의 추가 작업을 버리지 않는다. 관리 worktree와 그 안의 Git 제외 자료는 브랜치 정리만을 이유로 제거하지 않는다.

## 첫 정리 결과

병합된 PR 53개의 정확한 head와 develop 도달성을 확인하고 로컬 53개·원격 53개를 삭제했다. PR과 squash 이력은 유지된다. 다음 브랜치는 보존했다.

| 브랜치 | 이유 |
|---|---|
| docs/web-agent-handoff | 원래 작업 디렉터리에서 사용 중, 사용자 미추적 자료 보존 |
| feat/squad-web-ui | 별도 웹 담당 사용 여부를 확인하지 않았으므로 보존 |
| docs/llm-candidate-comparison | 현재 끝과 정확히 일치하는 병합 PR이 없음 |
| feat/position-api | 현재 끝과 정확히 일치하는 병합 PR이 없음 |
| feat/validated-agent-presentation | 정리 시 현재 작업 브랜치/PR #62 검증 중. 완료 후 별도 정리 |

## 기능 묶음 PR

필요 검사·현재 diff·제한을 확인한 후 head develop/base master로 생성한다. 이미 열린 동일 방향 PR이 있으면 갱신한다. Conventional Commits 영어 제목과 범위·검증·미완료 항목을 적고 공동저자/생성 도구 표기를 넣지 않는다. 생성만으로 master 반영/배포 완료를 보고하지 않는다. 기능 테스트가 통과해도 미구축 API·실데이터 단계·E2E를 완료로 표시하지 않는다.

현재 Python CI는 develop 대상 PR/푸시에서 실행하며 master 대상 PR에는 자동 실행되지 않는다. 해당 PR에는 develop의 실제 검증 SHA·결과와 적용 한계를 기록한다. master 병합 전 별도 검증 범위 확인이 필요하다.

## 후속 정리와 기능 묶음

최초 보류한 로컬 두 브랜치를 추가 조사했다. docs/llm-candidate-comparison의 끝 6319c7b는 PR #53의 최종 squash 커밋이고, feat/position-api의 끝 11213ec는 PR #32의 최종 squash 커밋이다. 둘 다 develop에 이미 포함됐고 별도 원격 브랜치·활성 작업·추가 커밋이 없어 로컬 두 개를 추가 정리했다. 처음 기록의 보류는 당시의 확인 상태이며 소급 삭제하지 않는다.

PR #62/#63/#64의 완료 작업 브랜치도 각각 병합 결과·현재 끝·사용 상태를 확인한 뒤 로컬·원격 정리했다. 최초 53개와 합쳐 현재 로컬 58개·원격 56개 정리다. 현재 사용 중인 이 문서 브랜치는 검증/병합 후 안전하게 분리해 삭제한다. 원래 작업 디렉터리의 docs/web-agent-handoff와 별도 웹 담당 feat/squad-web-ui는 보존한다.

기능 묶음 PR [#65](https://github.com/blue4683/fconline-recommendation-service/pull/65)는 develop→master 방향으로 생성했다. 내부 합성 Agent 기반/기존 엔진·정규화·읽기 API와 웹 모의 앱을 포함하고, 실제 모델 비교·Agent API/SSE·웹 실연결·실데이터 전체 완료를 주장하지 않는다. master 밖 두 merge commit은 추가 파일 차이가 없으며 merge-tree 결과가 develop과 일치함을 확인했다. master에는 병합하지 않았다. 이후 develop 변경은 PR head에 반영되므로 본문에 최신 검증 SHA와 제한을 갱신한다.

## 2026-10-22 실데이터 소스 통합 요청

사용자가 `feat/real-data → develop → master`의 검증·PR·Squash 병합을 명시적으로 요청했다. 이전 합성 전용 분리와 master 병합 미승인 기록보다 이번 요청을 우선한다. 최신 develop의 두 테스트 개선을 별도 통합 작업 브랜치에서 보존하고, 실데이터 브랜치는 이력을 재작성하지 않는 fast-forward로 준비한다. 이후 두 PR을 순서대로 검증·병합한다. CI는 master 대상도 검사한다.

이번 요청은 소스 통합 1회이며 배포·새 실제 실행·관측 자료/비밀값 커밋을 포함하지 않는다. 사용자 미추적 파일, 다른 담당 체크아웃, 기존 프로세스, 종료 실행과 잠금을 보존한다. 장기 브랜치 develop/master/feat/real-data는 삭제하지 않는다. [통합 기록](real-data-mainline-integration.md)을 따른다.
