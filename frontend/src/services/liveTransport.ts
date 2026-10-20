import type { StreamRequest } from '../contracts/webAgentV0';
import { TransportError, type Transport } from './transport';

/**
 * web-agent.v0 스트림 경로. INT-020 설계의 별도 합성 Agent 앱(127.0.0.1:8001) 경로와 같다.
 * 서버(FRS.api.agent_app)는 develop 00623ba부터 있다. 상주 서버가 켜져 있다고 가정하지 않으며, 화면은 GET /health로 먼저 확인한다.
 * 스트림 전 거절(403·408·409·413·415·422·503)은 상태 코드만 전달하고 본문은 해석하지 않는다.
 */
export const PROPOSED_STREAM_PATH = '/recommend/squad/stream';

type FetchLike = (input: string, init: RequestInit) => Promise<Response>;

export function normalizeBaseUrl(raw: string | undefined): string | null {
  const value = raw?.trim();
  if (!value) return null;
  let url: URL;
  try {
    url = new URL(value);
  } catch {
    return null;
  }
  if (url.protocol !== 'http:' && url.protocol !== 'https:') return null;
  if (url.search || url.hash || url.username || url.password) return null;
  return url.href.replace(/\/+$/, '');
}

export function createLiveTransport(baseUrl: string, fetchImpl: FetchLike = (i, init) => fetch(i, init)): Transport {
  const endpoint = `${baseUrl}${PROPOSED_STREAM_PATH}`;
  return {
    mode: 'live',
    async open(request: StreamRequest, signal: AbortSignal) {
      let response: Response;
      try {
        // 원문은 본문에만 싣고 query string에 넣지 않는다. 재시도하지 않는다.
        response = await fetchImpl(endpoint, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json', Accept: 'text/event-stream' },
          body: JSON.stringify(request),
          signal,
          cache: 'no-store',
          credentials: 'omit',
          redirect: 'error',
        });
      } catch (error) {
        if (signal.aborted) throw error;
        throw new TransportError(
          'network',
          '서버에 연결하지 못했습니다. 서버가 켜져 있는지, 이 화면을 허용된 주소(5173 개발 서버)에서 열었는지 확인해 주세요.',
        );
      }
      if (!response.ok) {
        throw new TransportError('http', `서버가 요청을 처리하지 못했습니다 (HTTP ${response.status}).`, response.status);
      }
      const type = response.headers.get('content-type') ?? '';
      if (!type.toLowerCase().startsWith('text/event-stream')) {
        throw new TransportError('content_type', '서버 응답이 이벤트 스트림 형식이 아닙니다.');
      }
      if (!response.body) throw new TransportError('no_body', '서버 응답 본문이 없습니다.');
      return response.body;
    },
  };
}
