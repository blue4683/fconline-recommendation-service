# R2 추출기 주입 그래프

2026-10-20, 웹 제외 백엔드 W1/R2 구현. 실제 LLM 없이 추출기를 주입하여 extract → normalize → 조건 충족 시 기존 build를 실행한다. 코드 경로 검증과 자연어 의미 정확도 측정을 구분한다.

## 실행 계약

- AgentDependencies에 추출기·MaterialProvider·BatchSelection·응답 clock을 명시한다. SquadAgent.run에는 원문과 시간대 있는 t0를 전달한다.
- 4,000 코드포인트 초과/비문자/공백 원문은 모델 호출 전에 기존 정규화 오류로 종료한다. 원문을 자르거나 치환하지 않는다.
- 추출기는 요청당 1번만 호출한다. ExtractionError는 안전한 extraction_error 코드로 반환하고 원래 예외/모델 초안을 공개하지 않는다. 예상 밖 프로그래밍 오류는 숨기지 않는다.
- 기존 정규화기의 모든 사유/근거/지원 범위를 유지한다. 문제가 있으면 t0/배치/자료/응답 시계/엔진에 접근하지 않는다.
- 정상 조건에서는 내부 배치/t0/clock을 검증한 뒤 공급기를 1번 읽고 기존 build_squad_request를 호출한다. 기본 CP-SAT 설정·독립 검증·상태·스냅샷 기한은 바꾸지 않는다.
- 그래프에 대화 이력·체크포인트·추출 재시도·최신 자료 선택·수집/학습·웹 전송을 추가하지 않는다. 각 호출의 상태는 독립이다.
- 반환값은 내부 AgentRunResult다. 그래프 종료 뒤 표/서술/HTTP를 추가할 때는 응답 직전 최종 만료 검사를 다시 구현해야 한다. 현재 내부 결과를 곧바로 웹 final로 보내는 계약이 아니다.

## 의존성과 추적 전송

[공식 LangGraph 개요](https://docs.langchain.com/oss/python/langgraph/overview)의 StateGraph/compile/invoke 구조와 [PyPI 1.2.16 메타데이터](https://pypi.org/project/langgraph/1.2.16/)의 Python >=3.10·의존성 범위를 확인했다. Python 3.11.15의 기존 고정 의존성을 유지한 채 langgraph 1.2.16과 직접 사용하는 langsmith 0.14.11 SDK를 선택했다.

새 패키지 22개를 requirements.txt에 고정했다. requirements.in에는 두 직접 의존성을 기록하고 requirements-dev.txt의 기존 runtime include를 유지한다. 기존 패키지를 업데이트/삭제하지 않았고 httpx와 기존 httpx2는 별도 네임스페이스로 설치한다. 계정 연결·유료 서비스·원문 업로드 승인이 아니다.

[공식 요청별 추적 제어](https://docs.langchain.com/langsmith/conditional-tracing)에 따라 그래프 생성/호출을 tracing_context(enabled=False)로 감싼다. 외부 tracing 환경 변수가 켜져 있어도 이 호출의 전송을 비활성화하며 전역 환경을 변경하지 않는다. 사용자 지정 외부 callback/실행 config를 run 입력으로 받지 않는다.

## 검증

신규 합성 23개가 세 클럽×세 포메이션 실제 CP-SAT/독립 검증·추출/자료 1회 호출·누락/미지원/충돌/근거 불일치의 조기 종료·잘못된 원문/추출 구조·예외 비노출/재시도 없음·예상 밖 오류·대화 상태 비누적·엔진 만료/해 없음 전달을 확인했다.

추적 환경을 활성화한 합성 테스트에서 추출기 실행 컨텍스트의 enabled=False와 httpx/httpx2/requests 전송 0을 확인했다. 기존 환경 점검 명령에도 FRS.agent.graph의 안전한 import를 포함했다. 전체 pytest 1,176개·Ruff 린트/포맷·환경 import·pip check가 통과했다. 설치 여부를 무시한 깨끗한 의존성 해석도 고정 파일로 성공했다. 실제 새 환경 설치는 최신 head CI에서 다시 확인한다.

## 미완료

실제 Ollama 10회 비교, 표/설명/사실 검사, API/SSE·동시성/취소 정책·웹 실제 연결은 후속이다. 테스트 대역의 성공률을 실제 LLM 추출 성능으로 보고하지 않는다. 웹 파일·실데이터·기존 학습 모델·넥슨 수집은 변경하지 않았다.
