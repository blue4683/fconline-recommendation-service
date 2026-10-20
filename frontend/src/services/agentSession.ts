import {
  CONTRACT_VERSION,
  ContractError,
  parseEnvelope,
  type AgentEvent,
  type FinalResult,
  type ProgressPayload,
  type StreamRequest,
} from '../contracts/webAgentV0';
import { SseParser } from './sse';
import { TransportError, type Transport } from './transport';

export type Outcome =
  | { kind: 'final'; result: FinalResult }
  | { kind: 'server_error'; code: string; message: string }
  | { kind: 'protocol_error'; detail: string }
  | { kind: 'transport_error'; detail: string; status?: number }
  | { kind: 'aborted' };

export interface SessionHandlers {
  onStarted?: () => void;
  onProgress?: (progress: ProgressPayload) => void;
}

export function newRequestId(): string {
  const c = globalThis.crypto;
  if (c && typeof c.randomUUID === 'function') return c.randomUUID();
  const bytes = new Uint8Array(16);
  c.getRandomValues(bytes);
  return Array.from(bytes, (b) => b.toString(16).padStart(2, '0')).join('');
}

export function buildRequest(message: string, requestId: string = newRequestId()): StreamRequest {
  // 원문은 치환·요약·정규화·trim 하지 않고 그대로 보낸다.
  return { contract_version: CONTRACT_VERSION, request_id: requestId, message };
}

/**
 * 요청 하나의 스트림을 끝까지 읽어 종료 결과 하나를 돌려준다.
 *
 * - started가 seq 0으로 먼저 와야 하고, seq는 1씩 연속 증가해야 한다.
 * - final 또는 error가 정확히 하나 와야 한다. 같은 읽기 단위에서 그 뒤 이벤트가 보이면 프로토콜 오류.
 * - 종료 이벤트 전 EOF는 통신 오류이며 해 없음으로 바꾸지 않는다.
 * - request_id가 다르면 현재 요청의 이벤트가 아니므로 프로토콜 오류.
 * - 재시도·재연결을 하지 않는다.
 */
export async function runStream(
  transport: Transport,
  request: StreamRequest,
  signal: AbortSignal,
  handlers: SessionHandlers = {},
): Promise<Outcome> {
  let stream: ReadableStream<Uint8Array>;
  try {
    stream = await transport.open(request, signal);
  } catch (error) {
    if (signal.aborted) return { kind: 'aborted' };
    if (error instanceof TransportError) return { kind: 'transport_error', detail: error.message, status: error.status };
    return { kind: 'transport_error', detail: '요청을 보내지 못했습니다.' };
  }

  const reader = stream.getReader();
  const parser = new SseParser();
  let expectedSeq = 0;
  const state: { terminal: Outcome | null } = { terminal: null };

  const handle = (event: AgentEvent): Outcome | null => {
    if (event.request_id !== request.request_id) throw new ContractError('event.request_id: 현재 요청과 다릅니다');
    if (state.terminal) throw new ContractError('event: 종료 이벤트 뒤에 이벤트가 왔습니다');
    if (event.seq !== expectedSeq) throw new ContractError(`event.seq: ${expectedSeq}이 필요하지만 ${event.seq}을 받았습니다`);
    if (expectedSeq === 0 && event.type !== 'started') throw new ContractError('event: 첫 이벤트는 started여야 합니다');
    if (expectedSeq > 0 && event.type === 'started') throw new ContractError('event: started가 중복되었습니다');
    expectedSeq += 1;
    switch (event.type) {
      case 'started':
        handlers.onStarted?.();
        return null;
      case 'progress':
        handlers.onProgress?.(event.payload);
        return null;
      case 'final':
        return { kind: 'final', result: event.payload };
      case 'error':
        return { kind: 'server_error', code: event.payload.code, message: event.payload.message };
    }
  };

  const consume = (messages: { event: string; data: string }[]) => {
    for (const m of messages) {
      const result = handle(parseEnvelope(m.event, m.data));
      if (result) state.terminal = result;
    }
  };

  try {
    for (;;) {
      let chunk: ReadableStreamReadResult<Uint8Array>;
      try {
        chunk = await reader.read();
      } catch {
        if (signal.aborted) return { kind: 'aborted' };
        return { kind: 'transport_error', detail: '스트림을 읽는 중 연결이 끊겼습니다.' };
      }
      if (signal.aborted) return { kind: 'aborted' };
      if (chunk.done) break;
      consume(parser.push(chunk.value));
      // 종료 이벤트와 같은 청크의 뒤 이벤트까지 검사한 뒤 읽기를 멈춘다(서버가 연결을 늦게 닫아도 대기하지 않음).
      if (state.terminal) return state.terminal;
    }
    consume(parser.end().messages);
    if (state.terminal) return state.terminal;
    return { kind: 'transport_error', detail: '결과를 받기 전에 스트림이 끝났습니다.' };
  } catch (error) {
    if (signal.aborted) return { kind: 'aborted' };
    if (error instanceof ContractError) return { kind: 'protocol_error', detail: error.message };
    if (error instanceof TypeError) return { kind: 'protocol_error', detail: '스트림이 올바른 UTF-8이 아닙니다' };
    return { kind: 'protocol_error', detail: '스트림을 해석하지 못했습니다' };
  } finally {
    reader.cancel().catch(() => undefined);
  }
}
