/**
 * GET /objectives 응답(agent-objectives.v1, docs/web-agent-v2-contract.md §2).
 * 서버가 시작 때 고정한 설정만 알린다. 브라우저는 목록을 고쳐 쓰거나 사용 가능 여부를 추측하지 않는다.
 * 모르는 필드·사유·스키마 버전은 거부한다.
 */
import { arr, exactKeys, fail, int, obj, oneOf, type Obj } from './webAgentV1';
import { DEFAULT_OBJECTIVE, MULTI_FACTOR, OBJECTIVE_IDS, parsePolicy, type ObjectiveId, type ObjectivePolicy } from './webAgentV2';

export const OBJECTIVES_SCHEMA = 'agent-objectives.v1';

export const OBJECTIVE_REASONS = ['not_configured', 'policy_mismatch', 'synthetic_material', 'agent_unavailable'] as const;
export type ObjectiveReason = (typeof OBJECTIVE_REASONS)[number];

/** 다요소 방식의 고정 탐색 설정. 표시용이며 브라우저가 요청에 싣지 않는다. */
export interface ObjectiveParameters {
  gk_exchange: number;
  outfield_tolerance: number;
  gk_tolerance: number;
  search_seconds: number;
}

export interface ObjectiveOption {
  id: ObjectiveId;
  available: boolean;
  reason: ObjectiveReason | null;
  policy: ObjectivePolicy | null;
  parameters: ObjectiveParameters | null;
}

export interface ObjectiveListing {
  default: ObjectiveId;
  objectives: ObjectiveOption[];
}

function parseParameters(value: unknown, path: string): ObjectiveParameters {
  const o = obj(value, path);
  exactKeys(o, path, ['gk_exchange', 'outfield_tolerance', 'gk_tolerance', 'search_seconds']);
  const seconds = o.search_seconds;
  // 서버는 초를 30.0처럼 실수로 직렬화할 수 있다. 양의 유한수만 받고 표시에만 쓴다.
  if (typeof seconds !== 'number' || !Number.isFinite(seconds) || seconds <= 0 || seconds > 3600) {
    fail(`${path}.search_seconds`, '양의 탐색 시간이 필요합니다');
  }
  return {
    gk_exchange: int(o.gk_exchange, `${path}.gk_exchange`, 0, 2000),
    outfield_tolerance: int(o.outfield_tolerance, `${path}.outfield_tolerance`, 0, 2000),
    gk_tolerance: int(o.gk_tolerance, `${path}.gk_tolerance`, 0, 200),
    search_seconds: seconds,
  };
}

function parseOption(value: unknown, path: string): ObjectiveOption {
  const o: Obj = obj(value, path);
  exactKeys(o, path, ['id', 'available', 'reason', 'policy', 'parameters']);
  const id = oneOf(o.id, OBJECTIVE_IDS, `${path}.id`);
  if (typeof o.available !== 'boolean') fail(`${path}.available`, '참/거짓이 필요합니다');
  const reason = o.reason === null ? null : oneOf(o.reason, OBJECTIVE_REASONS, `${path}.reason`);
  const policy = o.policy === null ? null : parsePolicy(o.policy, `${path}.policy`);
  const parameters = o.parameters === null ? null : parseParameters(o.parameters, `${path}.parameters`);

  if (o.available !== (reason === null)) fail(`${path}.reason`, '사용 불가일 때만 사유가 있어야 합니다');
  if (id === 'ovr') {
    if (policy !== null || parameters !== null) fail(path, 'OVR 방식에는 정책·설정이 없어야 합니다');
    if (reason !== null && reason !== 'agent_unavailable') fail(`${path}.reason`, 'OVR 방식은 서버 준비 실패로만 막힙니다');
  } else {
    if (parameters === null) fail(`${path}.parameters`, '다요소 방식에는 탐색 설정이 필요합니다');
    if (o.available !== (policy !== null)) fail(`${path}.policy`, '사용 가능한 다요소 방식에만 정책 출처가 있어야 합니다');
  }
  return { id, available: o.available, reason, policy, parameters };
}

/**
 * HTTP 상태와 본문을 함께 검사한다. 정상은 200, 서버 준비 실패(agent_unavailable)는 503이다.
 * 두 방식이 모두 정확히 한 번씩 있어야 하며 기본 방식은 OVR이다.
 */
export function parseObjectives(value: unknown, httpStatus: number): ObjectiveListing {
  const path = 'objectives';
  const o = obj(value, path);
  exactKeys(o, path, ['schema_version', 'default', 'objectives']);
  if (o.schema_version !== OBJECTIVES_SCHEMA) fail(`${path}.schema_version`, `${OBJECTIVES_SCHEMA}가 필요합니다`);
  const def = oneOf(o.default, OBJECTIVE_IDS, `${path}.default`);
  if (def !== DEFAULT_OBJECTIVE) fail(`${path}.default`, '기본 방식은 OVR이어야 합니다');
  const options = arr(o.objectives, `${path}.objectives`).map((v, i) => parseOption(v, `${path}.objectives[${i}]`));
  const ids = options.map((x) => x.id);
  if (ids.length !== OBJECTIVE_IDS.length || new Set(ids).size !== ids.length) {
    fail(`${path}.objectives`, '두 방식이 한 번씩 필요합니다');
  }
  const ovr = options.find((x) => x.id === 'ovr')!;
  const multi = options.find((x) => x.id === MULTI_FACTOR)!;
  const agentDown = ovr.reason === 'agent_unavailable';
  if (agentDown !== (multi.reason === 'agent_unavailable')) fail(`${path}.objectives`, '서버 준비 실패는 두 방식에 함께 표시되어야 합니다');
  if (agentDown !== (httpStatus === 503)) fail(path, '서버 준비 실패는 HTTP 503, 그 밖에는 HTTP 200이어야 합니다');
  if (!agentDown && httpStatus !== 200) fail(path, 'HTTP 200이어야 합니다');
  return { default: def, objectives: options };
}

export function optionFor(listing: ObjectiveListing | null, id: ObjectiveId): ObjectiveOption | null {
  return listing?.objectives.find((x) => x.id === id) ?? null;
}
