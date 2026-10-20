import { describe, expect, it } from 'vitest';
import { SseParser } from '../src/services/sse';

function feedBytes(text: string, size: number) {
  const parser = new SseParser();
  const bytes = new TextEncoder().encode(text);
  const out = [];
  for (let i = 0; i < bytes.length; i += size) out.push(...parser.push(bytes.slice(i, i + size)));
  const end = parser.end();
  return { out: [...out, ...end.messages], incomplete: end.incomplete };
}

describe('SseParser', () => {
  it('UTF-8 한글이 1바이트 단위로 나뉘어도 복원한다', () => {
    const text = 'event: progress\ndata: {"m":"스쿼드 검증 중 ⚽"}\n\n';
    for (const size of [1, 2, 3, 7]) {
      const { out, incomplete } = feedBytes(text, size);
      expect(out).toEqual([{ event: 'progress', data: '{"m":"스쿼드 검증 중 ⚽"}' }]);
      expect(incomplete).toBe(false);
    }
  });

  it('CRLF·CR·LF 줄 끝과 CRLF가 청크 경계에서 갈라진 경우를 처리한다', () => {
    const parser = new SseParser();
    const a = parser.pushText('event: a\r');
    const b = parser.pushText('\ndata: 1\r\n\r');
    const c = parser.pushText('\nevent: b\rdata: 2\r\r');
    expect([...a, ...b, ...c]).toEqual([
      { event: 'a', data: '1' },
      { event: 'b', data: '2' },
    ]);
  });

  it('여러 data 행을 줄바꿈으로 잇고, 한 청크의 여러 이벤트를 모두 낸다', () => {
    const parser = new SseParser();
    const out = parser.pushText('data: {"a":\ndata: 1}\n\nevent: x\ndata:2\n\n');
    expect(out).toEqual([
      { event: 'message', data: '{"a":\n1}' },
      { event: 'x', data: '2' },
    ]);
  });

  it('주석 heartbeat·id·retry·data 없는 블록은 이벤트를 만들지 않는다', () => {
    const parser = new SseParser();
    const out = parser.pushText(': ping\n\nid: 3\nretry: 10\n\nevent: only-name\n\n');
    expect(out).toEqual([]);
  });

  it('빈 줄로 끝나지 않은 마지막 이벤트는 내보내지 않고 미완성으로 알린다', () => {
    const { out, incomplete } = feedBytes('event: final\ndata: {}', 4);
    expect(out).toEqual([]);
    expect(incomplete).toBe(true);
  });

  it('잘린 UTF-8로 끝나면 오류를 던진다', () => {
    const parser = new SseParser();
    const bytes = new TextEncoder().encode('data: 한');
    parser.push(bytes.slice(0, bytes.length - 1));
    expect(() => parser.end()).toThrow(TypeError);
  });

  it('잘못된 UTF-8 바이트는 오류를 던진다', () => {
    const parser = new SseParser();
    expect(() => parser.push(new Uint8Array([0x64, 0x61, 0xff, 0x0a]))).toThrow(TypeError);
  });
});
