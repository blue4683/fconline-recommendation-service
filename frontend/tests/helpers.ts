import { CONTRACT_VERSION } from '../src/contracts/webAgentV1';
import type { Transport } from '../src/services/transport';

export const FIXED_NOW = Date.parse('2026-10-20T01:00:00Z');

export function envelope(requestId: string, seq: number, type: string, payload: unknown, extra: Record<string, unknown> = {}) {
  return JSON.stringify({ contract_version: CONTRACT_VERSION, request_id: requestId, seq, type, payload, ...extra });
}

export function sse(type: string, data: string): string {
  return `event: ${type}\ndata: ${data}\n\n`;
}

/** 문자열을 UTF-8 바이트로 바꾼 뒤 정해진 크기로 잘라 스트림으로 만든다. */
export function byteStream(text: string, size = 5): ReadableStream<Uint8Array> {
  const bytes = new TextEncoder().encode(text);
  let offset = 0;
  return new ReadableStream({
    pull(controller) {
      if (offset >= bytes.length) {
        controller.close();
        return;
      }
      controller.enqueue(bytes.slice(offset, offset + size));
      offset += size;
    },
  });
}

/** request_id를 받아 응답 텍스트를 만드는 가짜 transport. */
export function scriptedTransport(script: (requestId: string) => string, size = 5): Transport & { calls: number } {
  const t = {
    mode: 'mock' as const,
    calls: 0,
    async open(request: { request_id: string }) {
      t.calls += 1;
      return byteStream(script(request.request_id), size);
    },
  };
  return t;
}

/** 직접 제어하는 스트림. 테스트가 청크·종료 시점을 정한다. */
export function controlledTransport() {
  const opened: { requestId: string; push: (text: string) => void; close: () => void }[] = [];
  const transport: Transport = {
    mode: 'mock',
    async open(request) {
      let ctrl!: ReadableStreamDefaultController<Uint8Array>;
      const stream = new ReadableStream<Uint8Array>({
        start(c) {
          ctrl = c;
        },
      });
      const enc = new TextEncoder();
      opened.push({
        requestId: request.request_id,
        push: (text) => ctrl.enqueue(enc.encode(text)),
        close: () => ctrl.close(),
      });
      return stream;
    },
  };
  return { transport, opened };
}
