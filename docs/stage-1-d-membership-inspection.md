# Stage 1-D 공식 멤버 목록 소량 진단 — 오류로 중단

2026-10-19, 실데이터 경로. 가격 파서·관측 재고 코드는 [PR #14](https://github.com/blue4683/fconline-recommendation-service/pull/14)에서 검증 후 develop에 병합했다. 이후 사용자에게 승인받은 별도 소량 진단을 수행했다. Stage 1-D 전체는 여전히 미완료다.

## 승인 범위

- 새 진단 1회, 최대 6 HTTP 시도·순차 초당 최대 1회.
- 실패한 TeamColorDetail URL 재호출 없음. 저장된 공식 JS가 참조하는 별도 공개 목록 경로만 확인.
- 302/403/429 및 오류 즉시 중단. 재시도·자동 리다이렉트 추적 없음.
- HTTP 클라이언트 기본 UA, 로그인·출처 헤더 위장·프록시·netrc 없음.
- 원본·요청 기록은 Git 제외 로컬에 무기한 보관. 대량 가격 수집은 포함하지 않음.
- 제공자 허용(0-B) 미확정은 유지.

## 요청 근거와 한계

기존 저장 공식 `datacenter.teamcolor.js`에서 `teamcolorGetPlayerList`는 `#teacolorSearchform`을 serialize해 `/DataCenter/TeamColorPlayerList`를 GET한다. `teamcolorInitPlayerList`는 strPlayerName·strSeason·strPosition·n1Confederation·n4LeagueId·n4TeamId·n4NationId를 빈 값으로 만들고 teamcolorid를 지정한다. 정렬 기본값은 overallrating descending, salary descending이다.

진단은 이 코드에서 직접 확인한 항목만 입력했다. 팀컬러는 공식 목록에서 확인한 리버풀 ID 1001을 사용했다. 원본 공개 목록 HTML에는 `#teacolorSearchform`이 없고 상세 팝업도 오류 상태이므로, 슬라이더 등 **전체 폼 입력을 완전히 재현한 요청이라고 주장하지 않는다.** 이 진단은 알려진 입력만으로 정상 응답이 나오는지 확인한 것이며, 응답 실패 원인을 확정하는 실험이 아니다.

## 결과

| 항목 | 결과 |
|---|---|
| 실행 ID | stage-1-d-membership-inspection-20261019 |
| 실제 HTTP 시도 | **1 / 최대 6** |
| 메서드·경로 | GET /DataCenter/TeamColorPlayerList |
| 확인 대상 | 리버풀 teamcolorid=1001 |
| HTTP 상태 | **302** |
| 본문 링크 | bulletin.nexon.com/nxk/error.html?aspxerrorpath=/DataCenter/TeamColorPlayerList |
| 종료 상태 | **stopped**, unexpected_response |
| 멤버 데이터 확보 | 없음 |

첫 응답에서 즉시 중단했다. 오류 페이지를 따라가지 않았으며 같은 요청 재시도·다른 클럽 요청·헤더 변경도 하지 않았다. 이전의 TeamColorDetail도 다시 호출하지 않았다. 남은 5회를 계속 쓸 수 있는 정상 진행 상태로 취급하지 않는다.

근거는 `data/raw/stage-1-d-membership-inspection-20261019/manifest.json`과 `01-club-1001.body`다. 요청 입력·시각·원본 SHA-256·중단 원인을 기록했다. 원본은 커밋하지 않는다. 기존 `stage-1-d-inspection-20261018`도 stopped/6회 상태 그대로 보존했다.

## 판단

상세와 목록의 두 경로에서 오류 페이지 리다이렉트를 관측했지만, 요청 입력 누락·서버 문맥·일시적 서버 오류 등 원인은 구별하지 못했다. HTTP 403/429 차단이나 로그인 요구로 단정하지 않는다. 공식 사이트가 다른 환경에서도 동작하지 않는다고 확대 해석하지 않는다.

공식 멤버 자격과 전체 입력·응답 범위 확인은 불가 상태다. 빈 목록·불포함·자격 없음으로 변환하지 않고, 경력 재고를 자격 확정으로 승격하지 않는다. 같은 오류로 범위를 넓혀 추가 호출하지 않는다. 정상적인 공식 접근 근거를 확보한 후 별도 판단이 필요하다.

이번 PR은 승인·관찰 결과·한계를 기록하는 문서 변경이다. 수집 코드 기능 변경은 없으며 pytest를 다시 실행할 필요가 없어 로컬에서는 재실행하지 않았다. 문서 경로·기존 원본 manifest/해시·Git diff를 확인했다. 기존 코드의 pytest 180개·Ruff·환경 검사와 최종 원격 CI는 PR #14에서 통과했다.

## 승인된 후속 작업

사용자는 1-D 미완료를 유지하고 **외부 요청 없이 1-E 공통 후보 검색을 합성 데이터로 먼저 구현**하도록 선택했다. 기존 단계 순서보다 이 명시적 결정을 우선하며 이번 문서 PR에서 설계도 §5·§11과 개발 지침에 기록한다. 실데이터 경로는 유지하며 가상 선수·가상 수치로 검증한다. 조회 계약은 별도 질문 중이고, 신선도·스냅샷 자동 선택·배치 확장 규칙을 임의로 정하지 않는다.
