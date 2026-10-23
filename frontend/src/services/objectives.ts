import { parseObjectives, type ObjectiveListing } from '../contracts/agentObjectives';
import { ContractError } from '../contracts/webAgentV1';

export type ObjectivesCheck =
  | { kind: 'listed'; listing: ObjectiveListing }
  | { kind: 'unreachable' }
  | { kind: 'invalid'; detail: string };

export type ObjectivesChecker = (baseUrl: string, signal?: AbortSignal) => Promise<ObjectivesCheck>;

type FetchLike = (input: string, init: RequestInit) => Promise<Response>;

/**
 * GET {base}/objectives 한 번. 재시도·주기 조회를 하지 않는다. 200·503만 본문을 해석하고,
 * 그 밖의 상태(이 경로가 없는 이전 서버의 404 포함)·형식 오류는 invalid, 연결 실패는 unreachable이다.
 */
export function createObjectivesChecker(fetchImpl: FetchLike = (i, init) => fetch(i, init)): ObjectivesChecker {
  return async (baseUrl, signal) => {
    let response: Response;
    try {
      response = await fetchImpl(`${baseUrl}/objectives`, {
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
      return { kind: 'listed', listing: parseObjectives(await response.json(), response.status) };
    } catch (error) {
      if (error instanceof ContractError) return { kind: 'invalid', detail: error.message };
      return { kind: 'invalid', detail: '방식 목록 응답이 JSON이 아닙니다' };
    }
  };
}
