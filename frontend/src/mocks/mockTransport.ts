/**
 * 명시적으로 선택한 모의 응답. 원문을 해석하지 않고 고른 시나리오를 SSE 바이트로 재생한다.
 * 실제 파서를 그대로 통과하도록 이벤트·줄·UTF-8 경계와 무관하게 청크를 자르고,
 * CRLF·주석 heartbeat·여러 data 행을 섞는다.
 */
import { CONTRACT_VERSION, type StreamRequest } from '../contracts/webAgentV0';
import type { Transport } from '../services/transport';
import { finalFor, type ScenarioId } from './fixtures';

export interface MockOptions {
  scenario: () => ScenarioId;
  now?: () => number;
  /** 이벤트 사이 지연(ms). 테스트에서는 0. */
  delayMs?: number;
}

const STAGES: [string, string][] = [
  ['extract', '요청에서 조건을 찾고 있습니다'],
  ['normalize', '필수 조건을 확인하고 있습니다'],
  ['build', '스쿼드를 탐색하고 검증하고 있습니다'],
  ['render', '검증된 결과로 표를 만들고 있습니다'],
  ['narrate', '설명을 작성하고 있습니다'],
  ['check', '설명과 표의 수치를 대조하고 있습니다'],
];

type Frame = { type: string; payload: unknown; seq?: number };

function encodeEvent(requestId: string, seq: number, frame: Frame, variant: number): string {
  const json = JSON.stringify({
    contract_version: CONTRACT_VERSION,
    request_id: requestId,
    seq: frame.seq ?? seq,
    type: frame.type,
    payload: frame.payload,
  });
  const nl = variant % 2 === 0 ? '\n' : '\r\n';
  if (variant % 3 === 2) {
    // 같은 JSON을 두 data 행으로 나눈다. 파서는 \n으로 다시 이어 붙이며 JSON 공백으로 허용된다.
    const cut = json.indexOf(',');
    return `event: ${frame.type}${nl}data: ${json.slice(0, cut + 1)}${nl}data: ${json.slice(cut + 1)}${nl}${nl}`;
  }
  return `event: ${frame.type}${nl}data: ${json}${nl}${nl}`;
}

function frames(id: ScenarioId, now: number): Frame[] {
  const out: Frame[] = [{ type: 'started', payload: { data_mode: 'synthetic' } }];
  const progress = (n: number) => STAGES.slice(0, n).map(([stage, message]) => ({ type: 'progress', payload: { stage, message } }));
  switch (id) {
    case 'needs_clarification':
      out.push(...progress(2));
      break;
    case 'server_error':
      out.push(...progress(3), { type: 'error', payload: { code: 'engine_failed', message: '추천 계산 중 오류가 발생했습니다.' } });
      return out;
    case 'protocol_error':
      out.push(...progress(2), { type: 'progress', payload: { stage: 'build', message: '번호가 건너뛴 이벤트' }, seq: 7 });
      return out;
    case 'disconnect':
      out.push(...progress(3));
      return out;
    case 'internal_error':
    case 'llm_unavailable':
    case 'data_expired':
      out.push(...progress(3));
      break;
    default:
      out.push(...progress(STAGES.length));
  }
  out.push({ type: 'final', payload: finalFor(id, now) });
  return out;
}

const CHUNK_PATTERN = [7, 23, 3, 41, 11, 2, 64, 5, 17, 1, 97];

function sleep(ms: number, signal: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    if (signal.aborted) return reject(signal.reason);
    const t = setTimeout(resolve, ms);
    signal.addEventListener('abort', () => {
      clearTimeout(t);
      reject(signal.reason);
    }, { once: true });
  });
}

export function createMockTransport(options: MockOptions): Transport {
  const now = options.now ?? (() => Date.now());
  const delay = options.delayMs ?? 0;
  return {
    mode: 'mock',
    async open(request: StreamRequest, signal: AbortSignal) {
      const scenario = options.scenario();
      const list = frames(scenario, now());
      const encoder = new TextEncoder();
      let index = 0;
      let p = 0;
      return new ReadableStream<Uint8Array>({
        async pull(controller) {
          if (index >= list.length) {
            controller.close();
            return;
          }
          if (delay > 0) {
            try {
              await sleep(delay, signal);
            } catch (e) {
              controller.error(e);
              return;
            }
          }
          let text = encodeEvent(request.request_id, index, list[index]!, index);
          if (index === 1) text = `: heartbeat${'\n'}${text}`;
          const bytes = encoder.encode(text);
          for (let offset = 0; offset < bytes.length; ) {
            const size = CHUNK_PATTERN[p++ % CHUNK_PATTERN.length]!;
            controller.enqueue(bytes.slice(offset, offset + size));
            offset += size;
          }
          index += 1;
        },
      });
    },
  };
}
