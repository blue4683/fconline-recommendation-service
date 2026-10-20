# Stage 4-A 원문 구간·정확 금액 코드

2026-10-20. [승인 계약](stage-4-a-amount-contract.md)의 원문 구간 검증·순수 금액 파서만 구현했다. LLM·전체 요청 조립·강화/별칭 파서·DB/API·새 수집·실데이터 실행은 없다.

## 입출력과 보존

`FRS/core/request_normalization/amounts.py`와 패키지 공개 함수는 다음과 같다.

- `SourceSpan(start, end, text)`: 유니코드 코드포인트·0기반/end 미포함·엄격 정수·빈/역순/한도 밖 구간 거부. 원문을 정리하거나 text를 strip하지 않는다.
- `validate_source_span(original, span)`: 원문4,000자·형식/경계·정확한 부분 문자열 대조. 다른 출현 위치를 검색하거나 Unicode 정규화로 복구하지 않는다.
- `parse_amount(text)`: 금액100자·전체 토큰 소비·정확 문법/단위·정수BP·0~10^34-1을 검증해 int를 반환한다.
- `normalize_amount(original, span)`: 먼저 원문을 검증하고 `NormalizedAmount(span, bp, normalizer_version)`을 반환한다. 근거와 결과는 불변 객체이며 원문/숫자를 파일이나 로그에 저장하지 않는다.

잘못된 입력은 NormalizationError의 사유 코드로 구분한다. 원문 내용을 그 오류 메시지에 넣지 않는다. 구간 불일치/범위·길이/단위 생략·모호성/문법·정밀도/범위를 각각 처리하며 정상 숫자로 대체하지 않는다. SourceSpan 생성 자체의 잘못된 필드는 Pydantic 구조 검증 오류다. 생성 검증을 우회한 객체도 함수 진입 시 재검증한다.

```python
from FRS.core.request_normalization import SourceSpan, normalize_amount

original = "예산1조5천억"
span = SourceSpan(start=2, end=len(original), text=original[2:])
result = normalize_amount(original, span)
assert result.bp == 1_500_000_000_000
```

독립 규칙 버전은 `23ed1cc794eb4e960fbf7ceb6054943c5bb14e0f27a04749d10cd3a71005d3d0`이며 normalization_spec은 매번 별도 사본을 반환한다.

## 금액 문법과 정확성

ASCII 숫자·정상 3자리 쉼표 묶음·소수점 뒤 숫자를 토큰으로 검증한다. BP는 명시 단위다. 큰 단위 경/조/억/만/BP는 내림차순·중복 없이, 계수 안의 천/백/십도 내림차순·중복 없이 처리한다. 단위 앞 숫자를 생략하거나 한글 숫자로 대체하지 않는다. 공백은 토큰 사이에 허용하지만 숫자 두 개를 붙여 새 숫자로 만들지 않는다. 보이지 않는 비공백 문자·미지원 접미사·부호·지수/분수/밑줄을 일부만 읽어 승인하지 않는다.

`1조 5천억`, `1.5조`는1,500,000,000,000 BP, `150,000BP`는150,000 BP다. `1천2백34억`은123,400,000,000 BP, `1조5천BP`는1,000,000,005,000 BP다. `1000`과`1조5000`은 단위 생략 오류이며 BP/억을 보충하지 않는다. `1.2BP`나0.1BP에 해당하는 소수는 반올림하지 않고 거부한다.

[Python Fraction 문서](https://docs.python.org/3.11/library/fractions.html)에 따라 검증한 문자열을 유리수로 바꿔 합산한다. float를 거치거나 Decimal 기본 정밀도에 의존하지 않으며 최종 분모1 여부와 BP 범위를 확인한다. Fraction이 허용하는 다른 문법까지 입력 문법으로 확대하지 않는다. Python3.11.15/기존 의존성을 유지했다.

## 검증과 한계

신규 합성92개와 전체 pytest931개·Ruff 린트/포맷·환경 import·pip check를 통과했다. 같은 PR의 설계도 §11/인수인계에 기록했다. 합성 검증은34자리/2^53 초과/1BP 소수·단위 조합·단위 순서/중복·쉼표/공백·모호성/생략/미지원 토큰·정확한 길이 경계·원문 구간 위조/Unicode 인덱스·원본/사양 불변·파일/네트워크 I/O 부재를 포함한다.

이 파서는 명시된 금액 구간 하나를 확인한다. 원문이 `예산 1조 말고 2조`일 때 `1조` 부분이 실제로 존재한다는 사실만으로 의미상 예산이1조라고 보장하지 않는다. 구간 밖 충돌/부정·필수5조건·미지원 조건 누락의 검증과 4-D 의미 평가가 따로 필요하다. 현재 함수만으로 전체 자연어 요청이 완성되거나 스쿼드가 생성되지는 않는다.

후속 범위는 강화 집합/정식 값 정규화·전체 조건 조립·LLM 공급자/모델/비용·API 연결이며 이번 승인에 포함하지 않는다. 1-D/F·공식 발동/보너스·실데이터 추천과 운영 성능은 미완료다.
