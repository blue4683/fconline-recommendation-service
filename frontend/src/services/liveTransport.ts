import { TransportError, type AnyStreamRequest, type Transport } from './transport';

/**
 * web-agent.v1·v2 스트림 경로(같은 경로, 본문의 contract_version으로 구분). INT-020 설계의 별도 Agent 앱(127.0.0.1:8001) 경로와 같다.
 * 서버(FRS.api.agent_app)는 develop 00623ba부터 있다. 상주 서버가 켜져 있다고 가정하지 않으며, 화면은 GET /health로 먼저 확인한다.
 * 스트림 전 거절(403·408·409·413·415·422·503)은 상태 코드를 전달한다. 본문은 409의 detail.code만 읽는다
 * (agent_busy와 objective_unavailable 구분). 어느 경우에도 다시 보내거나 다른 방식으로 바꿔 보내지 않는다.
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

const REJECTION_CODE = /^[a-z][a-z0-9_]{0,63}$/;

/** 거절 본문 {"detail": {"code": "..."}}의 code. 형식이 다르면 undefined이며 화면은 상태 코드로만 안내한다. */
async function rejectionCode(response: Response): Promise<string | undefined> {
  try {
    const body: unknown = await response.json();
    if (typeof body !== 'object' || body === null) return undefined;
    const detail = (body as Record<string, unknown>).detail;
    if (typeof detail !== 'object' || detail === null) return undefined;
    const code = (detail as Record<string, unknown>).code;
    return typeof code === 'string' && REJECTION_CODE.test(code) ? code : undefined;
  } catch {
    return undefined;
  }
}

export function createLiveTransport(baseUrl: string, fetchImpl: FetchLike = (i, init) => fetch(i, init)): Transport {
  const endpoint = `${baseUrl}${PROPOSED_STREAM_PATH}`;
  return {
    mode: 'live',
    async open(request: AnyStreamRequest, signal: AbortSignal) {
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
        const code = response.status === 409 ? await rejectionCode(response) : undefined;
        throw new TransportError('http', `서버가 요청을 처리하지 못했습니다 (HTTP ${response.status}).`, response.status, code);
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
