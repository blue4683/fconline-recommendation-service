# W8 문서 Q&A·LangSmith 구현 기록

2026-10-21. 사용자 확정: RAG+LangSmith, 기존 qwen3:8b·로컬 검색의 별도 문서 Q&A API·CLI. [계약](rag-langsmith-contract.md)을 따른다. 기존 추천 엔진/Agent/웹/25문장 평가를 변경하지 않았다.

## 구현한 경로

- `FRS/knowledge/retrieval.py`: 허용된 세 Markdown의 구간 ID/해시/manifest 검증, 문자2~4gram TF-IDF cosine 검색 상위3개. 0점 구간은 제외한다. corpus 버전은 manifest에 고정한다. 임의 파일/디렉터리 전체를 인덱싱하지 않는다.
- `service.py`: 검색 다음에 qwen3:8b를 한 번 호출하고 답변/출처 ID를 검증한다. 답변 최대1,200자, 인용 최대3개, 미등록/검색하지 않은/중복 ID 거부. 근거 부족은 `insufficient_evidence`, 모델/스키마 실패는 `provider_error`다. 문서 Q&A가 스쿼드를 만들거나 가격/예산을 바꾸지 않는다.
- `observation.py`: retrieve/generate/validate의 시간·상태·제공된 토큰만 로컬에 기록한다. 원문/답변/예외 전문을 관측기로 넘기지 않는다. 실제 Ollama/oracle/test double의 종류를 구분하고 대역 생성을 실제 LLM trace로 표기하지 않는다.
- `tracing.py`: langsmith0.14.11의 실제 Client를 통해 root/child run을 수동 전송하는 선택적 어댑터. 기본0회, 배치/재시도/redirect/proxy 상속 없음. 명시한 미국/EU endpoint의 `/runs`만 허용한다. 전송 직전에 SDK 처리 결과와 검증된 payload를 재비교해 숨은 metadata 확장을 거부한다. 원문·답변·문서 본문·로컬 경로·런타임 환경·비밀값은 trace 본문/metadata에 넣지 않는다. API 키는 LangSmith 인증 헤더에만 사용한다.
- `api.py`: 추천 앱과 독립된 FastAPI. 요청 본문64KiB/질문4,000자/request_id64자, 동시1/대기0, 응답150초. 끊김/기한 뒤 후속 단계를 시작하지 않고 실행 중 작업이 끝나야 슬롯을 해제한다. 시작 설정이 없으면503이며 health 조회로 모델을 다시 호출하지 않는다.
- `evaluate.py`: 사전 정답7질문·대역 실행·새 실제1회 실행기. 기본은 계획 출력, 실제 실행은 별도 `--run`, 전송은 별도 `--upload-traces`. 호출 전에 pending을 fsync하고 종료/중단 잠금을 보존한다.

문서 검색만 성공한 결과는 생성형 RAG 성공으로 세지 않는다. SDK 대역 전송만으로 LangSmith 서버 수신을 확인했다고 보고하지 않는다. 아래 내용은 구현 직후의 검증 범위다. 이후 승인된 [실제 1회 결과](rag-langsmith-results.md)는 고정7질문5/7·LangSmith POST26회와 첫 root 읽기 확인1회다.

## 지식과 출처

[지원 조건](knowledge/support.md), [강화](knowledge/reinforcement.md), [검증/자료 범위](knowledge/verification.md)는 프로젝트 계약을 요약한 새 지식 문서다. 원본 계약/코드 링크를 각 파일에 남겼다. 배포·최신 공식 게임 규칙/시세를 보증하는 문서가 아니다. `knowledge/manifest.v1.json`의 파일별 SHA256과 corpus 버전을 함께 검증한다. 문서를 수정하면 새 hash/version을 명시적으로 기록해야 한다.

현재 corpus 버전: `cf55ddd5ddec9815891be445b2144b412ede40c6e3f07acc53f155f8db1b2b7e`. 출처 ID가 유효하다는 사실만으로 자유 서술 전체가 사실임을 보장하지 않는다. 별도 질문 평가와 실제 답변의 내용 검토가 필요하다.

## CLI와 HTTP 계약

아래 두 명령은 모델/metadata/외부 전송이 **0회**다.

```sh
python -m FRS.knowledge --question '5강과 8강 차이는?'
python -m FRS.knowledge.evaluate --offline
```

첫 명령은 검색 계획, 두 번째는 정답 대역으로 검색/구조/인용/상태를 검증한다. 대역7/7을 실제 모델의 정확도로 보고하지 않는다.

HTTP는 `FRS.knowledge.api:create_knowledge_app` 또는 `FRS.knowledge.api:app`을 사용한다. 이번 단계에서는 상주 서버/포트를 실행하지 않았다. 운영 주소는 수동 실행 시 지정한다. 실제 설정에는 `RAG_ENABLED=1`과 명시 모델 digest가 필요하며 시작 시 읽기 전용 모델 확인 최대2회가 발생한다. `.env.example`에는 비밀값 없는 키 이름만 추가했다. 키 존재만으로 tracing이 켜지지 않는다. 교재의 LANGCHAIN_API_KEY/ENDPOINT/PROJECT 별칭도 지원하며, 두 이름에 서로 다른 값이 있으면 거부한다. 사용자 설정 파일은 변경하지 않는다.

`POST /knowledge/query` 요청:

```json
{"request_id":"qa-001","question":"지원 포메이션은?"}
```

응답은 `knowledge-answer.v1`: `request_id`, `status`(answered/insufficient_evidence/provider_error), `answer`, `corpus_version`, `sources`, `trace_id`, `error_code`. sources는 검색한 source_id/document/title/text를 보존한다. 근거 부족과 오류에는 답변/출처를 채워 넣지 않는다. HTTP200이어도 도메인 status를 확인한다. 잘못된 JSON/구조422, 본문 초과413, content-type415, 미설정503, 실행 중409, 응답 기한504다. `GET /health`는 `knowledge-health.v1`의 준비 상태/corpus 버전을 반환한다. CORS는 기존 localhost/127.0.0.1:5173 두 origin만·credentials 없음이다.

웹 담당은 위 별도 계약을 확인한 뒤 UI를 연결한다. 기존 agent-response/SSE를 바꾸지 않았고 frontend/와 WEB-* 기록을 수정하지 않았다. 아직 이 새 Q&A의 실제 브라우저 검증은 없다.

## 실제 실행 전 고정한 미리보기

- 실행 ID: `w8-rag-20261021-001`, 승인 루트 `artifacts/knowledge-evaluation/approval-20261021-001`.
- 질문: `FRS/knowledge/cases.v1.json`의 정상4/근거 부족·무관·지시 무시3, 총7개. 상태·출처·필수 용어의 사전 검사를 고정했다. 키워드 검사는 완전한 의미 판정이 아니며 실제 답변을 별도로 검토한다.
- 상한: 로컬 모델 상태2회·추론7회(검색0점이면 미호출), 추론당120초·순차·재시도/다운로드 없음. 서버/연결 오류 즉시 중단.
- LangSmith 선택 전송 상한: run POST28회 + 첫 root 수신 확인 GET1회, 합계29회. 각 Q&A의 root와 완료된 단계만 전송한다. 중단/부분 전송/미상도 남기며 오류 후 후속 질의를 멈춘다.
- 원문/답변/검색 본문은 로컬에만 보관한다. 외부에는 임의 UUID·parent/trace ID·단계명·시각/지연·상태/오류 코드·제공된 토큰·corpus 해시·생성기 종류·설정한 프로젝트명만 전송한다. [합성 payload 미리보기](knowledge-trace-preview.json)는 대역 예시이며 실제 전송 기록이 아니다.
- 입력/출력은 `{}`이고 SDK runtime/env metadata를 끈다. LangSmith 계정의 보관/과금 정책을 로컬 무기한 D2와 같거나 무료라고 가정하지 않는다.
- 외부 수신은 생성 요청의2xx와 첫 root 읽기 확인을 구분한다. 모든 child의 UI 표시까지 읽어 확인한 것으로 과장하지 않는다.
- 이 문서는 실행 프로그램과 전송 항목을 검토할 수 있게 만든 기록이다. 새 실제 호출·전송 범위는 사용자에게 확인하며 기존 소진 승인을 재사용하지 않는다.

## 검증 상태와 한계

새 테스트는 검색 관련성/해시·허용 파일·출처 거부/근거 부족·한 번 호출·취소·HTTP 경계/슬롯·Ollama 대역·실제 LangSmith SDK의 대역 payload/전송 오류·기본 비활성·실행 잠금/미상 상한/소스 변경/부분 전송을 검증한다. 기본 테스트는 외부 네트워크를 막는다. 신규91개/전체 pytest1,662개가60.48초에 통과했다. Ruff lint/format(237파일)·환경 import·pip check도 통과했다. 대역7질문은7/7이며 모델/외부 전송0회다. PR의 최신 CI는 별도로 확인한다.

이 절은 실제 실행 전 상태다. 후속 실측에서 실제 모델5/7·LangSmith 첫 root 수신을 확인했고 실패2건을 남겼다. 새 Q&A 브라우저 연결은 미검증이다. 스쿼드25문장의 과거24/25를 RAG 성적이나 새 전체 시스템의 재평가로 사용하지 않는다. Multi-Agent와 실데이터1-D/1-F는 이번 작업으로 완료되지 않는다.

## 전달과 남은 실행

사용자 제공 설정 파일은 주 체크아웃의 Git 제외 .env다. LANGCHAIN_API_KEY/ENDPOINT/PROJECT로 설정되어 있어 비밀값을 출력하지 않고 호환성을 확인했다. 외부 대상은 미국 endpoint의 FRS 프로젝트다. 이 파일은 수정하거나 다른 작업 디렉터리로 복사하지 않았다. 구현 검증 시점의 실제 전송은0회였으며, 이후 새 승인1회를 실행한 결과는 별도 결과 문서를 따른다.

기준 develop은 작업 도중 다른 실행 주체의 문서 PR#96을 포함한 ecfb6fb로 진행됐다. 코드 변경이 없는 것을 확인하고 fast-forward로 최신화했다. WEB-007의 실데이터 설계는 별도 후속이며 이번 RAG/관측 작업에 새 수집·기존 자격 정책 변경을 편입하지 않았다.

## 실제 검증 후 상태 (2026-10-21)

PR#97은 CI39556588989 성공 후 baf6fab으로 develop Squash 병합했다. 새 사용자 승인1회의 [결과](rag-langsmith-results.md)는7질문 처리·추론6회·사전 검사5/7이며 K03 답변 누락/K05 상태별 형식 위반을 실패로 보존한다. LangSmith POST26 성공 응답과 첫 root GET200을 확인했다. 이번 Ollama는 종료했고 승인은 소진됐다. 기존 웹/엔진과 고정 corpus/정답/프롬프트를 바꾸지 않았다.

인계 후 다음 단계 요청으로 [출력 계약을 보완](model-output-contract-hardening.md)했다. 생성 프롬프트에 규칙 질문과 실제 구성 요청의 구분을 명시하고, 상태별 JSON Schema를 나눴다. 새 합성 회귀를 통과했으나 추가 실제 모델/metadata·LangSmith 요청은0회다. 위 실제5/7·실패2건과 원래 기록을 유지한다.
