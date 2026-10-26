/**
 * web-agent.v2 — 요청마다 고른 추천 방식과 다요소 점수를 더한 웹 계약 (docs/web-agent-v2-contract.md).
 * 서버 DTO(FRS/api/agent_contract.py WebFinalV2)처럼 v1 검사를 그대로 재사용하고 v2 필드만 따로 검사한다.
 * 맞지 않으면 보정하지 않고 거부한다. 점수는 서버 문자열을 그대로 쓰며 다시 계산·반올림하지 않는다.
 * 대조에 필요한 정수 연산은 BigInt로만 한다(부동소수점 미사용).
 */
import {
  CONTRACT_VERSION,
  arr,
  exactKeys,
  fail,
  int,
  obj,
  oneOf,
  parseEnvelopeAs,
  parseFinal,
  str,
  type EnvelopeOf,
  type FinalResult,
  type Obj,
  type Squad,
  type SquadRow,
} from './webAgentV1';

export const CONTRACT_VERSION_V2 = 'web-agent.v2';

export const OBJECTIVE_IDS = ['ovr', 'gk_priority_multi_factor'] as const;
export type ObjectiveId = (typeof OBJECTIVE_IDS)[number];
export const DEFAULT_OBJECTIVE: ObjectiveId = 'ovr';
export const MULTI_FACTOR: ObjectiveId = 'gk_priority_multi_factor';

export const STAGE_NAMES = ['outfield_ovr', 'gk_ovr', 'multi_factor'] as const;
export type StageName = (typeof STAGE_NAMES)[number];
export const STAGE_STATUSES = ['optimal', 'incomplete', 'not_run'] as const;
export type StageStatus = (typeof STAGE_STATUSES)[number];

/** 선수 1명 다요소 점수 상한(1unit=0.01점)과 11명 합계 상한. 서버 DTO와 같다. */
export const MAX_SCORE_UNITS = 10000;
export const MAX_TOTAL_SCORE_UNITS = 110000;

export interface StreamRequestV2 {
  contract_version: typeof CONTRACT_VERSION_V2;
  request_id: string;
  message: string;
  objective: ObjectiveId;
}

export interface SquadRowV2 extends SquadRow {
  score_units: number | null;
  score: string | null;
}

export interface SquadV2 extends Squad {
  rows: SquadRowV2[];
  outfield_ovr: number;
  gk_ovr: number;
  total_score_units: number | null;
  total_score: string | null;
}

export interface ObjectivePolicy {
  policy_id: string;
  version: string;
  status: 'experimental';
}

export interface ObjectiveStage {
  name: StageName;
  status: StageStatus;
  value: number | null;
}

export interface ObjectiveResult {
  id: ObjectiveId;
  policy: ObjectivePolicy | null;
  stages: ObjectiveStage[];
}

export interface FinalResultV2 extends Omit<FinalResult, 'squad'> {
  squad: SquadV2 | null;
  objective: ObjectiveResult;
}

export type AgentEventV2 = EnvelopeOf<FinalResultV2>;

// ---------------------------------------------------------------------------
// 점수 표기

const SCORE_TEXT = /^(0|[1-9][0-9]*)\.[0-9]{2}$/;
const HEX64 = /^[0-9a-f]{64}$/;

/**
 * 정수 점수(1unit=0.01점)의 정확한 두 자리 표기. 서버 `_score_text`와 같은 정수 나눗셈이다.
 * 표시는 서버 문자열을 쓰고, 이 함수는 대조와 단계 요약 값(문자열이 오지 않는 정수)에만 쓴다.
 */
export function scoreText(units: number): string {
  const u = BigInt(units);
  return `${u / 100n}.${(u % 100n).toString().padStart(2, '0')}`;
}

function scoreUnits(value: unknown, path: string, max: number): number | null {
  return value === null ? null : int(value, path, 0, max);
}

function scoreString(value: unknown, path: string): string | null {
  if (value === null) return null;
  const s = str(value, path);
  if (!SCORE_TEXT.test(s)) fail(path, '소수점 아래 두 자리의 점수 문자열이 필요합니다');
  return s;
}

export function parsePolicy(value: unknown, path: string): ObjectivePolicy {
  const o = obj(value, path);
  exactKeys(o, path, ['policy_id', 'version', 'status']);
  const version = str(o.version, `${path}.version`);
  if (!HEX64.test(version)) fail(`${path}.version`, '64자리 16진 정책 해시가 필요합니다');
  return {
    policy_id: str(o.policy_id, `${path}.policy_id`, { nonEmpty: true }),
    version,
    status: oneOf(o.status, ['experimental'] as const, `${path}.status`),
  };
}

function parseStage(value: unknown, path: string): ObjectiveStage {
  const o = obj(value, path);
  exactKeys(o, path, ['name', 'status', 'value']);
  return {
    name: oneOf(o.name, STAGE_NAMES, `${path}.name`),
    status: oneOf(o.status, STAGE_STATUSES, `${path}.status`),
    value: scoreUnits(o.value, `${path}.value`, MAX_TOTAL_SCORE_UNITS),
  };
}

function parseObjective(value: unknown, path: string): ObjectiveResult {
  const o = obj(value, path);
  exactKeys(o, path, ['id', 'policy', 'stages']);
  const id = oneOf(o.id, OBJECTIVE_IDS, `${path}.id`);
  const policy = o.policy === null ? null : parsePolicy(o.policy, `${path}.policy`);
  const stages = arr(o.stages, `${path}.stages`).map((s, i) => parseStage(s, `${path}.stages[${i}]`));
  if (id === 'ovr' && (policy !== null || stages.length > 0)) fail(path, 'OVR 방식에는 정책·단계가 없어야 합니다');
  if (id !== 'ovr' && policy === null) fail(`${path}.policy`, '다요소 방식에는 정책 출처가 필요합니다');
  return { id, policy, stages };
}

/** v2 전용 키가 모두 있는지 확인하고, v1 검사에 넘길 사본에서 뺀다. */
function split(o: Obj, path: string, keys: readonly string[]): { rest: Obj; extra: Obj } {
  const rest: Obj = { ...o };
  const extra: Obj = {};
  for (const key of keys) {
    if (!Object.prototype.hasOwnProperty.call(o, key)) fail(`${path}.${key}`, '필수 필드가 없습니다');
    extra[key] = o[key];
    delete rest[key];
  }
  return { rest, extra };
}

const ROW_V2 = ['score_units', 'score'] as const;
const SQUAD_V2 = ['outfield_ovr', 'gk_ovr', 'total_score_units', 'total_score'] as const;

/**
 * final payload 검사(v2). 서버 WebFinalV2와 같은 순서다.
 * 1) v2 필드를 떼어 v1 parseFinal로 기존 불변 조건(11명·자리·합계·상한·상태 구조)을 검사한다.
 * 2) 점수 표기·합계, 필드/GK OVR 분리, 방식별 정책·단계 구조를 검사한다.
 */
export function parseFinalV2(value: unknown): FinalResultV2 {
  const path = 'final';
  const o = obj(value, path);
  const top = split(o, path, ['objective']);
  const versions = obj(o.versions, `${path}.versions`);
  if (versions.web_contract !== CONTRACT_VERSION_V2) fail(`${path}.versions.web_contract`, `${CONTRACT_VERSION_V2}이 필요합니다`);

  let squadExtra: Obj | null = null;
  let rowExtras: Obj[] = [];
  const v1Body: Obj = { ...top.rest, versions: { ...versions, web_contract: CONTRACT_VERSION } };
  if (o.squad !== null && o.squad !== undefined) {
    const squadPath = `${path}.squad`;
    const squad = split(obj(o.squad, squadPath), squadPath, SQUAD_V2);
    squadExtra = squad.extra;
    const rows = arr(squad.rest.rows, `${squadPath}.rows`).map((r, i) =>
      split(obj(r, `${squadPath}.rows[${i}]`), `${squadPath}.rows[${i}]`, ROW_V2),
    );
    rowExtras = rows.map((r) => r.extra);
    v1Body.squad = { ...squad.rest, rows: rows.map((r) => r.rest) };
  }
  const base = parseFinal(v1Body);
  const objective = parseObjective(top.extra.objective, `${path}.objective`);

  let squad: SquadV2 | null = null;
  if (base.squad !== null && squadExtra !== null) {
    const sp = `${path}.squad`;
    const rows: SquadRowV2[] = base.squad.rows.map((row, i) => {
      const rp = `${sp}.rows[${i}]`;
      const units = scoreUnits(rowExtras[i]!.score_units, `${rp}.score_units`, MAX_SCORE_UNITS);
      const score = scoreString(rowExtras[i]!.score, `${rp}.score`);
      if ((units === null) !== (score === null)) fail(rp, '점수와 정수 점수는 함께 있거나 함께 없어야 합니다');
      if (units !== null && score !== scoreText(units)) fail(`${rp}.score`, '정수 점수의 정확한 두 자리 표기가 아닙니다');
      return { ...row, score_units: units, score };
    });
    const outfield_ovr = int(squadExtra.outfield_ovr, `${sp}.outfield_ovr`, 0, 2000);
    const gk_ovr = int(squadExtra.gk_ovr, `${sp}.gk_ovr`, 0, 200);
    const total_score_units = scoreUnits(squadExtra.total_score_units, `${sp}.total_score_units`, MAX_TOTAL_SCORE_UNITS);
    const total_score = scoreString(squadExtra.total_score, `${sp}.total_score`);

    const keepers = rows.filter((r) => r.slot === 'GK');
    if (keepers.length !== 1 || keepers[0]!.raw_ovr !== gk_ovr) fail(`${sp}.gk_ovr`, 'GK 행의 OVR과 일치하지 않습니다');
    if (outfield_ovr + gk_ovr !== base.squad.total_ovr) fail(`${sp}.outfield_ovr`, '필드와 GK OVR의 합이 OVR 합과 다릅니다');

    const scored = rows.map((r) => r.score_units);
    if (total_score_units === null) {
      if (scored.some((s) => s !== null) || total_score !== null) fail(sp, 'OVR 방식에는 다요소 점수가 없어야 합니다');
    } else {
      if (scored.some((s) => s === null)) fail(sp, '다요소 방식에는 모든 선수의 점수가 필요합니다');
      const sum = scored.reduce<bigint>((acc, s) => acc + BigInt(s!), 0n);
      if (sum !== BigInt(total_score_units)) fail(`${sp}.total_score_units`, '행 점수 합과 일치하지 않습니다');
      if (total_score !== scoreText(total_score_units)) fail(`${sp}.total_score`, '정수 합계의 정확한 두 자리 표기가 아닙니다');
    }
    squad = { ...base.squad, rows, outfield_ovr, gk_ovr, total_score_units, total_score };
  }

  const success = base.status === 'optimal' || base.status === 'feasible';
  const multiFactor = objective.id !== 'ovr';
  if (squad !== null && multiFactor !== (squad.total_score_units !== null)) {
    fail(`${path}.squad`, '방식과 다요소 점수 유무가 일치하지 않습니다');
  }
  if (objective.stages.length > 0 && !(success && multiFactor)) {
    fail(`${path}.objective.stages`, '단계 요약은 다요소 성공 결과에만 있습니다');
  }
  if (success && multiFactor) {
    // 서버는 세 단계를 실행 순서(필드 OVR → GK OVR → 다요소)로 보낸다. 웹은 이 순서만 받는다.
    const names = objective.stages.map((s) => s.name).join();
    if (names !== STAGE_NAMES.join()) fail(`${path}.objective.stages`, '필드 OVR·GK OVR·다요소 세 단계가 순서대로 필요합니다');
  }

  return { ...base, versions: { ...base.versions, web_contract: CONTRACT_VERSION_V2 }, squad, objective };
}

/** v2 SSE 한 이벤트. envelope의 contract_version도 web-agent.v2여야 한다. */
export function parseEnvelopeV2(eventName: string, data: string): AgentEventV2 {
  return parseEnvelopeAs(eventName, data, CONTRACT_VERSION_V2, parseFinalV2);
}

export function isFinalV2(result: FinalResult | FinalResultV2): result is FinalResultV2 {
  return Object.prototype.hasOwnProperty.call(result, 'objective');
}
