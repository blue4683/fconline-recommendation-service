/**
 * 요청 입력 안내용 예시. 브라우저는 이 형식을 검사하거나 원문을 고치지 않는다.
 *
 * 백엔드 후보 준비(FRS/agent/candidates.py, develop 7c56bbc)는 쉼표로 나눈 정형 조건문을 받는다.
 * 아래 세 문장은 그 시점 백엔드의 후보 준비·정규화에서 ready를 확인했다(WEB-003).
 * 반대로 "예산 6조 BP", "급여 210 이하", "리버풀 4-2-3-1"처럼 절을 합치거나 꾸밈말을 붙이면 되묻기가 된다.
 */
export const REQUEST_EXAMPLES: readonly string[] = [
  '예산 6조, 리버풀, 4-2-3-1, 5~8강으로 구성해줘',
  '예산 5조 8000억, 첼시, 4-1-2-3, 5강,7강으로 구성해줘',
  '예산 3조, 바이에른 뮌헨, 4-2-2-2, 6~11강으로 구성해줘',
];

/** 조건별 작성 형식 안내. 지원 범위가 바뀌면 서버의 supported 값이 기준이다. */
export const REQUEST_FORMAT: readonly { label: string; example: string }[] = [
  { label: '예산', example: '예산 6조, 예산 5조 8000억 (끝에 BP를 붙이지 않음)' },
  { label: '급여 상한', example: '310 고정 · 입력 생략 가능 (다른 값은 수정 요청)' },
  { label: '클럽', example: '리버풀, 첼시, 바이에른 뮌헨' },
  { label: '포메이션', example: '4-2-3-1, 4-1-2-3, 4-2-2-2' },
  { label: '강화', example: '7강, 5~8강, 5강,7강 (5~11강)' },
];
