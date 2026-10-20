/**
 * text/event-stream 증분 파서.
 *
 * 청크는 이벤트·줄·한글(UTF-8 다중 바이트) 경계와 일치한다고 가정하지 않는다.
 * CRLF·CR·LF 줄 끝, 여러 data 행, 빈 줄 종료, 주석(heartbeat) 행을 처리한다.
 * id/retry 필드는 무시한다. 자동 재연결은 이 클라이언트에서 사용하지 않는다.
 */

export interface SseMessage {
  event: string;
  data: string;
}

export class SseParser {
  private readonly decoder = new TextDecoder('utf-8', { fatal: true });
  private buffer = '';
  private eventName = '';
  private dataLines: string[] = [];
  private hasData = false;
  private sawCr = false;
  private firstChunk = true;

  /** 바이트 청크를 넣고 완성된 이벤트를 반환한다. 잘못된 UTF-8은 TypeError를 던진다. */
  push(chunk: Uint8Array): SseMessage[] {
    return this.feed(this.decoder.decode(chunk, { stream: true }));
  }

  /** 이미 디코딩된 문자열 청크(테스트·모의용). */
  pushText(text: string): SseMessage[] {
    return this.feed(text);
  }

  /**
   * 스트림 끝. 디코더에 남은 바이트가 있으면 TypeError.
   * incomplete=true는 빈 줄로 끝나지 않은 미완성 이벤트가 남았다는 뜻이다(사양상 폐기).
   */
  end(): { incomplete: boolean; messages: SseMessage[] } {
    const rest = this.decoder.decode();
    const messages = rest ? this.feed(rest) : [];
    const incomplete = this.buffer.length > 0 || this.hasData || this.eventName !== '';
    this.buffer = '';
    this.resetEvent();
    return { incomplete, messages };
  }

  private feed(text: string): SseMessage[] {
    if (this.firstChunk && text.length > 0) {
      this.firstChunk = false;
      if (text.charCodeAt(0) === 0xfeff) text = text.slice(1);
    }
    const out: SseMessage[] = [];
    for (const ch of text) {
      if (this.sawCr) {
        this.sawCr = false;
        if (ch === '\n') continue; // CRLF의 LF
      }
      if (ch === '\r' || ch === '\n') {
        if (ch === '\r') this.sawCr = true;
        const line = this.buffer;
        this.buffer = '';
        const msg = this.line(line);
        if (msg) out.push(msg);
      } else {
        this.buffer += ch;
      }
    }
    return out;
  }

  private line(line: string): SseMessage | null {
    if (line === '') {
      if (!this.hasData) {
        this.resetEvent();
        return null;
      }
      const msg = { event: this.eventName || 'message', data: this.dataLines.join('\n') };
      this.resetEvent();
      return msg;
    }
    if (line.startsWith(':')) return null; // 주석·heartbeat
    const colon = line.indexOf(':');
    const field = colon === -1 ? line : line.slice(0, colon);
    let value = colon === -1 ? '' : line.slice(colon + 1);
    if (value.startsWith(' ')) value = value.slice(1);
    if (field === 'event') this.eventName = value;
    else if (field === 'data') {
      this.dataLines.push(value);
      this.hasData = true;
    }
    return null;
  }

  private resetEvent(): void {
    this.eventName = '';
    this.dataLines = [];
    this.hasData = false;
  }
}
