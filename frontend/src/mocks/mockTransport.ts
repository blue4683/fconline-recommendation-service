/**
 * 명시적으로 선택한 모의 응답. 원문을 해석하지 않고 고른 시나리오를 SSE 바이트로 재생한다.
 * 실제 파서를 그대로 통과하도록 이벤트·줄·UTF-8 경계와 무관하게 청크를 자르고,
 * CRLF·주석 heartbeat·여러 data 행을 섞는다.
 */
import { optionFor, type ObjectiveListing } from '../contracts/agentObjectives';
import { CONTRACT_VERSION_V2 } from '../contracts/webAgentV2';
import { TransportError, type AnyStreamRequest, type Transport } from '../services/transport';
import { finalFor, finalForV2, type ScenarioId } from './fixtures';

export interface MockOptions {
  scenario: () => ScenarioId;
  now?: () => number;
  /** 이벤트 사이 지연(ms). 테스트에서는 0. */
  delayMs?: number;
  /** 모의 방식 목록. 사용할 수 없는 방식을 요청하면 서버처럼 실행 전에 HTTP 409로 거절한다. */
  listing?: () => ObjectiveListing | null;
}

const OBJECTIVE_UNAVAILABLE = () =>
  new TransportError('http', '서버가 요청을 처리하지 못했습니다 (HTTP 409).', 409, 'objective_unavailable');

/** 백엔드 W4-1(FRS/api/agent_events.py PROGRESS)이 실제로 보내는 두 단계. 내부 세부 단계를 꾸며 내지 않는다. */
const STAGES: [string, string][] = [
  ['agent', '조건 확인과 합성 스쿼드 구성을 시작합니다.'],
  ['render', '결과와 응답 기한을 확인합니다.'],
];

/** 백엔드 공개 오류 코드와 안내(FRS/api/agent_projection.py ERROR_MESSAGES 중 일부). */
const ERRORS: Record<string, string> = {
  selection_provider_error: '조건 추출 모델을 사용할 수 없습니다. 잠시 후 새 요청을 보내 주세요.',
  request_deadline: '응답 기한을 초과해 추천을 반환하지 않습니다.',
};

type Frame = { type: string; payload: unknown; seq?: number };

function encodeEvent(request: AnyStreamRequest, seq: number, frame: Frame, variant: number): string {
  const json = JSON.stringify({
    contract_version: request.contract_version,
    request_id: request.request_id,
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

function frames(id: ScenarioId, now: number, request: AnyStreamRequest): Frame[] {
  const out: Frame[] = [{ type: 'started', payload: { data_mode: 'synthetic' } }];
  const progress = (n: number) => STAGES.slice(0, n).map(([stage, message]) => ({ type: 'progress', payload: { stage, message } }));
  switch (id) {
    case 'selection_error':
      out.push(...progress(1), { type: 'error', payload: { code: 'selection_provider_error', message: ERRORS.selection_provider_error } });
      return out;
    case 'request_deadline':
      out.push(...progress(1), { type: 'error', payload: { code: 'request_deadline', message: ERRORS.request_deadline } });
      return out;
    case 'protocol_error':
      out.push(...progress(1), { type: 'progress', payload: { stage: 'render', message: '번호가 건너뛴 이벤트' }, seq: 7 });
      return out;
    case 'disconnect':
      out.push(...progress(1));
      return out;
    default:
      out.push(...progress(STAGES.length));
  }
  const payload = request.contract_version === CONTRACT_VERSION_V2 ? finalForV2(id, now, request.objective) : finalFor(id, now);
  out.push({ type: 'final', payload });
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
    async open(request: AnyStreamRequest, signal: AbortSignal) {
      const scenario = options.scenario();
      if (scenario === 'objective_unavailable') throw OBJECTIVE_UNAVAILABLE();
      if (request.contract_version === CONTRACT_VERSION_V2 && options.listing) {
        if (optionFor(options.listing(), request.objective)?.available !== true) throw OBJECTIVE_UNAVAILABLE();
      }
      const list = frames(scenario, now(), request);
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
          let text = encodeEvent(request, index, list[index]!, index);
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
