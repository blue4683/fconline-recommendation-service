import { ContractError } from '../contracts/webAgentV1';
import { parseHealth, type AgentHealth } from '../contracts/agentHealth';

export type HealthCheck =
  | { kind: 'ready'; health: AgentHealth }
  | { kind: 'unavailable'; health: AgentHealth }
  | { kind: 'unreachable' }
  | { kind: 'invalid'; detail: string };

export type HealthChecker = (baseUrl: string, signal?: AbortSignal) => Promise<HealthCheck>;

type FetchLike = (input: string, init: RequestInit) => Promise<Response>;

/**
 * GET {base}/health 한 번. 재시도·주기 조회를 하지 않는다. 200(ready)·503(unavailable)만 본문을 해석하고,
 * 그 밖의 상태·형식 오류는 invalid, 연결 실패(서버 꺼짐·CORS 거부 포함)는 unreachable이다.
 */
export function createHealthChecker(fetchImpl: FetchLike = (i, init) => fetch(i, init)): HealthChecker {
  return async (baseUrl, signal) => {
    let response: Response;
    try {
      response = await fetchImpl(`${baseUrl}/health`, {
        method: 'GET',
        headers: { Accept: 'application/json' },
        cache: 'no-store',
        credentials: 'omit',
        redirect: 'error',
        signal,
      });
    } catch (error) {
      if (signal?.aborted) throw error;
      return { kind: 'unreachable' };
    }
    if (response.status !== 200 && response.status !== 503) {
      return { kind: 'invalid', detail: `예상하지 않은 HTTP ${response.status}` };
    }
    try {
      const health = parseHealth(await response.json(), response.status);
      return health.status === 'ready' ? { kind: 'ready', health } : { kind: 'unavailable', health };
    } catch (error) {
      if (error instanceof ContractError) return { kind: 'invalid', detail: error.message };
      return { kind: 'invalid', detail: '상태 응답이 JSON이 아닙니다' };
    }
  };
}
