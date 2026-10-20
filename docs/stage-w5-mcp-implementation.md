# W5 로컬 stdio MCP 구현·합성 검증

2026-10-21 / 합성 Agent 데모. [확정 계약](stage-w5-mcp-contract.md)을 기준으로 공식 mcp2.3.0·Python3.11.15의 Tool/Resource/Prompt와 저장소 내부 단일 전송 클라이언트를 구현했다. 기존 HTTP/웹·엔진·그래프·표 생성 코드는 수정하지 않았다. 실제 모델 호출·외부 호스트 등록·HTTP MCP·배포·넥슨 수집은 없다.

## 구성과 실행

- `FRS/mcp/server.py`: build_squad Tool, frs://supported-conditions Resource, select_conditions Prompt. 공식 SDK2의 MCPServer/lifespan/ClientSession과 stdio를 사용한다.
- `contract.py`: mcp-build.v1·엄격 입력/출력 스키마, 정확한 BP/급여/ID 문자열·원래 스냅샷/최종 만료 대조, 명시 합성 spec·배치/material ID·정책 버전. structuredContent와 text JSON은 같은 bytes에서 만든다. 기술 오류는 isError+안전 코드, 준비 문제/정규화 명확화는 도메인 결과다.
- `service.py`: 변경 없는 원문+candidate-selection.v1을 기존 registry/ID/정규화 계약으로 검증하고 기존 그래프/스냅샷·CP-SAT·독립 검증/코드 presentation을 재사용한다. 서버 설정과 다른 합성 material은 거부한다. 합성 자료는 조건이 정상인 첫 구성 때만 생성해 고정하며 Resource/Prompt/준비·선택·정규화 오류에서는 자료/시계/엔진을 사용하지 않는다.
- 단일 ThreadPoolExecutor·Gate/Lease·응답150초·CP10초·대기0·취소/기한 뒤 실제 future 종료까지 슬롯 유지. Gate/Lease는 기존 HTTP 모듈에서 읽기 전용으로 재사용한다. 그 모듈 import는 HTTP 앱을 등록할 뿐 lifespan/서버/모델을 시작하지 않는다. 프로토콜 오류·연결 종료·예상치 못한 오류를 직접 엔진 재호출로 복구하지 않는다.
- `client.py`의 call_build는 SDK의 공개 session.call_tool을 한 번만 사용한다. SDK2 고수준 Client.call_tool에는 HEADER_MISMATCH의 re-list/재전송 경로가 있어 사용하지 않는다. input-required 다중 라운드/extension 결과도 자동 처리하지 않는다. 메타데이터 discovery/결과 스키마 조회와 Tool 재실행을 구분한다. 임의의 외부 호스트가 재전송하지 않는다는 보장은 아니며 이번에는 등록하지 않았다.

실제 모델은 Tool/Resource/Prompt 어느 경로에서도 호출하지 않는다. 출력 model은 not_invoked_client_selection, presentation의 model_metadata는 not_provided다. Prompt는 현재 후보/바인딩된 JSON Schema/기존 지침을 제공할 뿐 모델 tool-use나 그래프의 MCP 전송 교체 완료를 뜻하지 않는다.

서버는 직접 선택한 run ID·시간대 포함 관측 시각을 명시해 시작한다. 다음은 명령 형식이며, 이번 구현 중 지속 서버를 켜두지 않았다.

```sh
.venv/bin/python -m FRS.mcp --run-id <명시-run-id> --observed-at <시간대-포함-ISO8601>
```

seed123·계열당3명은 고정, 추천 강화5~11이다. 시각 자동 갱신/최신 자료 선택/모델 다운로드·조회는 없다. stdout은 JSON-RPC 전용이고 진단은 stderr다. 서버 실행 프로세스를 종료하면 해당 stdio 연결도 종료된다. Resource에는 정적 지원값만 있고 Prompt에는 원문/후보가 있으므로 원문을 외부 호스트/LLM에 전달하는 연결은 별도 범위다.

## 검증 결과

신규50개/전체pytest1,490개·Ruff lint/format·환경 import·pip check 통과. SDK 신규 의존성13개와 기존 사용 httpx2의 정확한 버전을 고정했고, 기존 고정 패키지는 변경하지 않았다. MCP 의존성 closure25개에 미고정 패키지가 없음을 확인했다. macOS arm64 설치/import와 Linux CPython3.11용 신규14개 wheel 확보를 확인했으며 Linux 실행 결과는 해당 PR의 CI로 따로 확인한다. CLI extra는 설치하지 않았다.

주요 검증은 다음과 같다.

- 세 클럽×세 포메이션9개: 동일 원문/ID/배치/고정 시각에서 직접/MCP 결과 전체 일치·최적11명·자리/강화/조건·합계/모델 미호출 표시.
- 준비·미지원·필수 누락·예산 충돌·변조 ID/누락/중복/잘못된 필드/set_id·형식/길이/UTF-8·64KiB 경계·안전 오류. 문제 입력에서 자료·시계·엔진 접근 차단.
- 34자리 BP 문자열·해 없음/미확정·성공 뒤 만료로 표 제거·원래 기한/스냅샷 불변·합계/출처 변조·원래 snapshot 없는 성공 거부. 최초 구현의 마지막 만료 체크가 반환 상태를 읽지 못한 문제를 고쳐 회귀 테스트를 추가했다.
- Task 취소·150초 경과·서비스 종료 뒤 실제 worker가 끝나기 전 busy 유지·후속 단계 차단·정상 종료 후 슬롯 해제. SDK 클라이언트 취소도 실제 슬롯을 즉시 비우지 않는지 확인.
- Resource/Prompt가 모델·자료를 읽지 않음, Prompt 원문/후보 hash/schema 바인딩, 서버 Tool 출력 스키마·text/structured 일치·메타데이터 표시.
- 실제 stdio subprocess의 discovery/프로토콜2026-07-28·Tool/Resource/Prompt·고정 context 성공/직접 대조·EOF 정상 종료 marker·잘못된 CLI 설정의 stdout 오염 없음.
- 동일 request_id는 결과 캐시 없이 다시 실행한다. 전송 실패/기한/잘못된 결과/HEADER_MISMATCH에서 단일 전송 클라이언트의 Tool 전송1회·고수준 retry/fallback 미호출.

별도 재현 스크립트는 서버/클라이언트 subprocess를 만들어 과거 고정 시각의 합성 대조만 실행한다. 현재 추천 신선도 검증이 아니며 모델 선택 대역을 실제 정확도로 해석하지 않는다.

```sh
PYTHONPATH=. .venv/bin/python scripts/validate_mcp_stdio.py \
  --run-id mcp-stdio-comparison-20261021 \
  --observed-at 2026-10-21T00:00:00+00:00 \
  --output artifacts/<새-결과-디렉터리>
```

mcp-stdio-comparison-20261021-002에서 공식 SDK2.3.0/프로토콜2026-07-28로 정상9·누락/충돌2·준비 미지원1, 총12개 결과가 직접 호출과 전체 일치했다. source/plan/manifest와 스크립트·배치·고정 clock을 보존한다. 임시 subprocess 종료 marker도 확인했다. 첫001은 동일 대조를 고수준 클라이언트로 수행한 개발 중 기록이며 원본을 보존했다. 최종002는 단일 전송 client를 사용했고 실제 모델/HTTP 요청0·Tool fallback0이다.

Git 제외 `artifacts/mcp-stdio-comparison-20261021-002/`의 해시:

| 파일 | SHA256 |
|---|---|
| plan.json | d7c49d908db20a8a0ac489378618253e2d05baf7d0bcbfe5cd85fce90e7057e4 |
| manifest.json | 97878e3da0d684bf7d5b2f3e6f18cfa972a21b232bd0ac038610d427bd120a53 |
| sources.json | 98e6465abfc87d9ba56535ddf3f703a751d91024d464f895b65713decea43c7c |

mcp_policy_version=`68875a3efbd58301736159acfd5b82d30e18de6b94a7ec6d8c3850b573821136`이다. 모델/수집 원본·실행 결과·wheel은 커밋하지 않고 로컬 무기한 보관한다. 설치/검증 로그는 artifacts/mcp-* 경로다.

## 남은 한계

기존 브라우저는 직접 HTTP 경로를 사용하며 MCP를 쓰도록 변경하지 않았다. 호스트 등록·실제 LLM의 MCP 도구 선택·legacy host·HTTP MCP·다중 프로세스 슬롯/분산 중복방지·운영 SLA는 미검증이다. 64KiB 검사는 SDK가 읽고 파싱한 애플리케이션 인자 기준으로, stdio 전체 버퍼/DOS 방어가 아니다. Resource/Prompt의 정적 읽기와 solver 슬롯은 별개다. 프로세스 종료/손실 시 exactly-once·강제 취소를 보장하지 않는다. 합성 자료 밖 추천/공식 보너스·1-D/1-F 완료로 표시하지 않는다.

다음 W6의 평가25문장·반복수/기준·실제 추가 모델 호출·서술 비교·외부 관측은 확정 전 제안이다. 실제 제출 일정도 사용자 결정이 필요하다. 공식 SDK/프로토콜 근거는 확정 계약 문서의 링크를 따른다.
