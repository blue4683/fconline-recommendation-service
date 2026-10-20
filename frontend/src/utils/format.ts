/**
 * 표시 전용 문자열 처리. 정확한 10진 문자열을 Number로 바꾸지 않고 자릿수만 다룬다.
 */

/** Python의 len(str)과 같은 코드포인트 개수. JS의 .length(UTF-16 단위)와 다르다. */
export function codePointLength(text: string): number {
  let n = 0;
  for (const _ of text) n += 1;
  return n;
}

/** 공백뿐인 원문인지. 전송 원문 자체는 trim하지 않는다. */
export function isBlank(text: string): boolean {
  return text.trim().length === 0;
}

const DECIMAL = /^(0|[1-9][0-9]*)$/;

/** '1234567' → '1,234,567'. 10진 문자열이 아니면 그대로 돌려준다. */
export function groupDigits(decimal: string): string {
  if (!DECIMAL.test(decimal)) return decimal;
  return decimal.replace(/\B(?=(\d{3})+(?!\d))/g, ',');
}

const UNITS = ['', '만', '억', '조', '경', '해', '자', '양', '구'];

/**
 * BP 금액을 만 단위로 읽기 쉽게 표기한다. '5630700000000' → '5조 6,307억'.
 * 0인 단위는 생략하고 문자열 연산만 사용한다. 단위 범위를 넘으면 쉼표 표기를 돌려준다.
 */
export function koreanUnits(decimal: string): string {
  if (!DECIMAL.test(decimal)) return decimal;
  if (decimal === '0') return '0';
  const groups: string[] = [];
  for (let end = decimal.length; end > 0; end -= 4) groups.unshift(decimal.slice(Math.max(0, end - 4), end));
  if (groups.length > UNITS.length) return groupDigits(decimal);
  const parts: string[] = [];
  groups.forEach((g, i) => {
    const unit = UNITS[groups.length - 1 - i]!;
    const trimmed = g.replace(/^0+/, '');
    if (trimmed) parts.push(`${groupDigits(trimmed)}${unit}`);
  });
  return parts.join(' ');
}

const TIME_FORMAT = new Intl.DateTimeFormat('ko-KR', {
  year: 'numeric',
  month: '2-digit',
  day: '2-digit',
  hour: '2-digit',
  minute: '2-digit',
  second: '2-digit',
  hour12: false,
  timeZoneName: 'short',
});

export function formatTime(iso: string): string {
  const t = Date.parse(iso);
  return Number.isNaN(t) ? iso : TIME_FORMAT.format(t);
}

/** 0.5 단위 점수 표시. 표시 외의 계산에 쓰지 않는다. */
export function formatScore(value: number): string {
  return Number.isInteger(value) ? String(value) : value.toFixed(1);
}
