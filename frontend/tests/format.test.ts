import { describe, expect, it } from 'vitest';
import { codePointLength, groupDigits, isBlank, koreanUnits } from '../src/utils/format';

describe('codePointLength', () => {
  it('Python len과 같이 코드포인트로 센다 (이모지 1자)', () => {
    expect(codePointLength('⚽😀가')).toBe(3);
    expect('😀'.length).toBe(2);
    expect(codePointLength('😀'.repeat(4000))).toBe(4000);
    expect(codePointLength('\ud800')).toBe(1); // 짝 없는 서로게이트도 1
  });

  it('공백 판정은 원문을 바꾸지 않는다', () => {
    const text = ' \n\t ';
    expect(isBlank(text)).toBe(true);
    expect(text).toBe(' \n\t ');
    expect(isBlank(' a ')).toBe(false);
  });
});

describe('숫자 문자열 표시', () => {
  it('Number 변환 없이 자릿수를 묶는다', () => {
    expect(groupDigits('9007199254740993')).toBe('9,007,199,254,740,993');
    expect(groupDigits('9999999999999999999999999999999999')).toBe('9,999,999,999,999,999,999,999,999,999,999,999');
    expect(groupDigits('0')).toBe('0');
    expect(groupDigits('abc')).toBe('abc');
  });

  it('만 단위 한국어 표기', () => {
    expect(koreanUnits('5630700000000')).toBe('5조 6,307억');
    expect(koreanUnits('6000000000000')).toBe('6조');
    expect(koreanUnits('100000001')).toBe('1억 1');
    expect(koreanUnits('9999999999999999999999999999999999')).toMatch(/^99구 /);
  });
});
