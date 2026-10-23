/**
 * web-agent.v1 — 실데이터 브랜치의 확정 웹 계약 (real-data-branch-design.md §12.2).
 * 합성/관측 모드를 구분하며 서버 독립 검증기를 대신하지 않는다.
 * 값을 보정하지 않고 맞지 않으면 거부한다. develop/master의 v0는 변경하지 않는다.
 *
 * 금액·급여·합계·ID는 정확한 10진 문자열로 다룬다. Number/parseFloat로 변환하지 않는다.
 * 비교가 필요한 곳은 BigInt로 문자열을 정확히 읽어 대조만 한다.
 */

export const CONTRACT_VERSION = 'web-agent.v1';
export const DATA_MODES = ['synthetic', 'observed'] as const;
export type DataMode = (typeof DATA_MODES)[number];

/** 서버 원문 한도(Python 코드포인트 기준). */
export const MAX_MESSAGE_CODE_POINTS = 4000;

/** 기존 정규화 범위: BP 최대 34자리, 급여 2^63-1. */
export const MAX_BP_DIGITS = 34;
export const MAX_SALARY = 2n ** 63n - 1n;

export const SUPPORTED_CLUBS = ['리버풀', '첼시', '바이에른 뮌헨'] as const;
export const SUPPORTED_REINFORCEMENTS = [5, 6, 7, 8, 9, 10, 11] as const;

/** FRS/schema/scope.py의 FORMATION_SLOTS와 같은 표시 순서. 표 대조용이며 배치 계산이 아니다. */
export const FORMATION_SLOTS: Readonly<Record<string, readonly string[]>> = {
  '4-2-3-1': ['GK', 'RCB', 'LCB', 'RB', 'LB', 'RDM', 'LDM', 'CAM', 'RAM', 'LAM', 'ST'],
  '4-1-2-3': ['GK', 'RCB', 'LCB', 'RB', 'LB', 'CDM', 'RCM', 'LCM', 'ST', 'RW', 'LW'],
  '4-2-2-2': ['GK', 'RCB', 'LCB', 'RB', 'LB', 'RDM', 'LDM', 'RM', 'LM', 'RS', 'LS'],
};

export const FINAL_STATUSES = [
  'needs_clarification',
  'optimal',
  'feasible',
  'infeasible',
  'unknown',
  'data_expired',
  'internal_error',
  'llm_unavailable',
] as const;
export type FinalStatus = (typeof FINAL_STATUSES)[number];

export const EVENT_TYPES = ['started', 'progress', 'final', 'error'] as const;
export type EventType = (typeof EVENT_TYPES)[number];

export interface StreamRequest {
  contract_version: typeof CONTRACT_VERSION;
  request_id: string;
  message: string;
}

export interface Conditions {
  price_cap: string;
  salary_cap: string;
  team_color: string;
  formation: string;
  allowed_reinforcements: number[];
}

export interface ConditionIssue {
  field: string;
  kind: 'missing' | 'invalid' | 'unsupported' | 'ambiguous';
  code: string;
  message: string;
  occurrence: number | null;
}

export interface Supported {
  required_fields: string[];
  clubs: string[];
  formations: string[];
  reinforcements: number[];
}

export interface SquadRow {
  slot: string;
  spid: string;
  person_id: string;
  name: string;
  reinforce_level: number;
  price: string;
  salary: string;
  raw_ovr: number;
  performance: number;
  placement_type: string;
}

export interface Squad {
  rows: SquadRow[];
  total_price: string;
  total_salary: string;
  total_ovr: number;
  performance: number;
}

export interface Snapshot {
  id: string;
  t0: string;
  valid_until: string | null;
}

export type NarrationSource = 'llm_checked' | 'template' | 'none';

export interface FinalResult {
  status: FinalStatus;
  conditions: Conditions | null;
  issues: ConditionIssue[];
  supported: Supported;
  squad: Squad | null;
  snapshot: Snapshot | null;
  versions: Record<string, string>;
  answer: string | null;
  narration_source: NarrationSource;
  data_mode: DataMode;
}

export interface StartedPayload {
  data_mode: DataMode;
}
export interface ProgressPayload {
  stage: string;
  message: string;
}
export interface ErrorPayload {
  code: string;
  message: string;
}

export type AgentEvent =
  | { type: 'started'; seq: number; request_id: string; payload: StartedPayload }
  | { type: 'progress'; seq: number; request_id: string; payload: ProgressPayload }
  | { type: 'final'; seq: number; request_id: string; payload: FinalResult }
  | { type: 'error'; seq: number; request_id: string; payload: ErrorPayload };

/** 계약 구조가 맞지 않을 때 던진다. 메시지는 화면에 그대로 노출해도 안전한 설명만 담는다. */
export class ContractError extends Error {
  constructor(message: string) {
    super(message);
    this.name = 'ContractError';
  }
}

// ---------------------------------------------------------------------------
// 원시 검사 도우미

export type Obj = Record<string, unknown>;

export function fail(path: string, reason: string): never {
  throw new ContractError(`${path}: ${reason}`);
}

export function obj(value: unknown, path: string): Obj {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) fail(path, '객체가 필요합니다');
  return value as Obj;
}

export function exactKeys(o: Obj, path: string, required: readonly string[], optional: readonly string[] = []): void {
  for (const key of required) {
    if (!Object.prototype.hasOwnProperty.call(o, key)) fail(`${path}.${key}`, '필수 필드가 없습니다');
  }
  const allowed = new Set([...required, ...optional]);
  for (const key of Object.keys(o)) {
    if (!allowed.has(key)) fail(`${path}.${key}`, '알 수 없는 필드입니다');
  }
}

export function str(value: unknown, path: string, { nonEmpty = false } = {}): string {
  if (typeof value !== 'string') fail(path, '문자열이 필요합니다');
  if (nonEmpty && value.length === 0) fail(path, '빈 문자열은 허용되지 않습니다');
  return value;
}

export function int(value: unknown, path: string, min: number, max: number): number {
  if (typeof value !== 'number' || !Number.isSafeInteger(value)) fail(path, '정수가 필요합니다');
  if (value < min || value > max) fail(path, `${min}~${max} 범위가 필요합니다`);
  return value;
}

/** 0.5 단위 점수. 정수 OVR/2 범위만 허용한다. */
function halfStep(value: unknown, path: string, max: number): number {
  if (typeof value !== 'number' || !Number.isFinite(value)) fail(path, '숫자가 필요합니다');
  if (value < 0 || value > max || !Number.isInteger(value * 2)) fail(path, '0.5 단위의 범위 내 점수가 필요합니다');
  return value;
}

const DECIMAL = /^(0|[1-9][0-9]*)$/;

/** 정확한 비음수 10진 정수 문자열. JSON number는 이미 정밀도를 잃었을 수 있으므로 거부한다. */
export function decimalString(value: unknown, path: string, maxDigits?: number, max?: bigint): string {
  if (typeof value !== 'string') fail(path, '정확한 10진 문자열이 필요합니다 (숫자형 금지)');
  if (!DECIMAL.test(value)) fail(path, '부호·소수·공백 없는 10진 정수 문자열이 필요합니다');
  if (maxDigits !== undefined && value.length > maxDigits) fail(path, `${maxDigits}자리를 초과했습니다`);
  if (max !== undefined && BigInt(value) > max) fail(path, '허용 범위를 초과했습니다');
  return value;
}

const BP = (v: unknown, p: string) => decimalString(v, p, MAX_BP_DIGITS);
const SALARY = (v: unknown, p: string) => decimalString(v, p, 19, MAX_SALARY);
const ID = (v: unknown, p: string) => decimalString(v, p, 30);

/** 시간대가 있는 ISO 8601 시각. 브라우저 시계로 유효성을 확정하지 않으며 형식만 검사한다. */
const ISO_WITH_ZONE = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,9})?(Z|[+-]\d{2}:\d{2})$/;
export function isoTime(value: unknown, path: string): string {
  const s = str(value, path);
  if (!ISO_WITH_ZONE.test(s) || Number.isNaN(Date.parse(s))) fail(path, '시간대가 있는 ISO 8601 시각이 필요합니다');
  return s;
}

export function oneOf<T extends string>(value: unknown, allowed: readonly T[], path: string): T {
  if (typeof value !== 'string' || !(allowed as readonly string[]).includes(value)) {
    fail(path, `허용 값(${allowed.join(', ')})이 아닙니다`);
  }
  return value as T;
}

export function arr(value: unknown, path: string): unknown[] {
  if (!Array.isArray(value)) fail(path, '배열이 필요합니다');
  return value;
}

// ---------------------------------------------------------------------------
// payload 검사

function parseConditions(value: unknown, path: string): Conditions {
  const o = obj(value, path);
  exactKeys(o, path, ['price_cap', 'salary_cap', 'team_color', 'formation', 'allowed_reinforcements']);
  const levels = arr(o.allowed_reinforcements, `${path}.allowed_reinforcements`).map((v, i) =>
    int(v, `${path}.allowed_reinforcements[${i}]`, 5, 11),
  );
  if (levels.length === 0) fail(`${path}.allowed_reinforcements`, '비어 있을 수 없습니다');
  for (let i = 1; i < levels.length; i++) {
    if (levels[i]! <= levels[i - 1]!) fail(`${path}.allowed_reinforcements`, '중복 없는 오름차순이 필요합니다');
  }
  const formation = str(o.formation, `${path}.formation`, { nonEmpty: true });
  if (!Object.prototype.hasOwnProperty.call(FORMATION_SLOTS, formation)) fail(`${path}.formation`, '정식 지원 포메이션이 아닙니다');
  const team = str(o.team_color, `${path}.team_color`, { nonEmpty: true });
  if (!(SUPPORTED_CLUBS as readonly string[]).includes(team)) fail(`${path}.team_color`, '정식 지원 클럽이 아닙니다');
  return {
    price_cap: BP(o.price_cap, `${path}.price_cap`),
    salary_cap: SALARY(o.salary_cap, `${path}.salary_cap`),
    team_color: team,
    formation,
    allowed_reinforcements: levels,
  };
}

function parseIssue(value: unknown, path: string): ConditionIssue {
  const o = obj(value, path);
  exactKeys(o, path, ['field', 'kind', 'code', 'message', 'occurrence']);
  const occurrence = o.occurrence === null ? null : int(o.occurrence, `${path}.occurrence`, 0, Number.MAX_SAFE_INTEGER);
  return {
    field: str(o.field, `${path}.field`, { nonEmpty: true }),
    kind: oneOf(o.kind, ['missing', 'invalid', 'unsupported', 'ambiguous'] as const, `${path}.kind`),
    code: str(o.code, `${path}.code`, { nonEmpty: true }),
    message: str(o.message, `${path}.message`, { nonEmpty: true }),
    occurrence,
  };
}

function strList(value: unknown, path: string): string[] {
  return arr(value, path).map((v, i) => str(v, `${path}[${i}]`, { nonEmpty: true }));
}

function parseSupported(value: unknown, path: string): Supported {
  const o = obj(value, path);
  exactKeys(o, path, ['required_fields', 'clubs', 'formations', 'reinforcements']);
  return {
    required_fields: strList(o.required_fields, `${path}.required_fields`),
    clubs: strList(o.clubs, `${path}.clubs`),
    formations: strList(o.formations, `${path}.formations`),
    reinforcements: arr(o.reinforcements, `${path}.reinforcements`).map((v, i) =>
      int(v, `${path}.reinforcements[${i}]`, 1, 99),
    ),
  };
}

function parseRow(value: unknown, path: string): SquadRow {
  const o = obj(value, path);
  exactKeys(o, path, [
    'slot',
    'spid',
    'person_id',
    'name',
    'reinforce_level',
    'price',
    'salary',
    'raw_ovr',
    'performance',
    'placement_type',
  ]);
  return {
    slot: str(o.slot, `${path}.slot`, { nonEmpty: true }),
    spid: ID(o.spid, `${path}.spid`),
    person_id: ID(o.person_id, `${path}.person_id`),
    name: str(o.name, `${path}.name`, { nonEmpty: true }),
    reinforce_level: int(o.reinforce_level, `${path}.reinforce_level`, 5, 11),
    price: BP(o.price, `${path}.price`),
    salary: SALARY(o.salary, `${path}.salary`),
    raw_ovr: int(o.raw_ovr, `${path}.raw_ovr`, 0, 200),
    performance: halfStep(o.performance, `${path}.performance`, 100),
    placement_type: str(o.placement_type, `${path}.placement_type`, { nonEmpty: true }),
  };
}

function parseSquad(value: unknown, path: string, conditions: Conditions): Squad {
  const o = obj(value, path);
  exactKeys(o, path, ['rows', 'total_price', 'total_salary', 'total_ovr', 'performance']);
  const rows = arr(o.rows, `${path}.rows`).map((r, i) => parseRow(r, `${path}.rows[${i}]`));
  const squad: Squad = {
    rows,
    total_price: decimalString(o.total_price, `${path}.total_price`, MAX_BP_DIGITS + 2),
    total_salary: decimalString(o.total_salary, `${path}.total_salary`, 21),
    total_ovr: int(o.total_ovr, `${path}.total_ovr`, 0, 2200),
    performance: halfStep(o.performance, `${path}.performance`, 1100),
  };

  // 표시 경계 대조. 서버 검증기를 대신하지 않으며, 불일치는 보정하지 않고 거부한다.
  const expectedSlots = FORMATION_SLOTS[conditions.formation]!;
  if (rows.length !== 11) fail(`${path}.rows`, '11명이 필요합니다');
  const slots = rows.map((r) => r.slot);
  if ([...slots].sort().join() !== [...expectedSlots].sort().join()) fail(`${path}.rows`, '포메이션 자리와 일치하지 않습니다');
  if (new Set(rows.map((r) => r.person_id)).size !== 11) fail(`${path}.rows`, '같은 인물이 중복되었습니다');
  if (new Set(rows.map((r) => r.spid)).size !== 11) fail(`${path}.rows`, '같은 카드가 중복되었습니다');
  for (const [i, r] of rows.entries()) {
    if (!conditions.allowed_reinforcements.includes(r.reinforce_level)) {
      fail(`${path}.rows[${i}].reinforce_level`, '허용 강화 집합 밖입니다');
    }
  }
  const sumPrice = rows.reduce((acc, r) => acc + BigInt(r.price), 0n);
  const sumSalary = rows.reduce((acc, r) => acc + BigInt(r.salary), 0n);
  const sumOvr = rows.reduce((acc, r) => acc + r.raw_ovr, 0);
  if (sumPrice.toString() !== squad.total_price) fail(`${path}.total_price`, '행 합계와 일치하지 않습니다');
  if (sumSalary.toString() !== squad.total_salary) fail(`${path}.total_salary`, '행 합계와 일치하지 않습니다');
  if (sumOvr !== squad.total_ovr) fail(`${path}.total_ovr`, '행 합계와 일치하지 않습니다');
  if (sumPrice > BigInt(conditions.price_cap)) fail(`${path}.total_price`, '예산 상한을 초과합니다');
  if (sumSalary > BigInt(conditions.salary_cap)) fail(`${path}.total_salary`, '급여 상한을 초과합니다');
  return squad;
}

function parseSnapshot(value: unknown, path: string): Snapshot {
  const o = obj(value, path);
  exactKeys(o, path, ['id', 't0', 'valid_until']);
  return {
    id: str(o.id, `${path}.id`, { nonEmpty: true }),
    t0: isoTime(o.t0, `${path}.t0`),
    valid_until: o.valid_until === null ? null : isoTime(o.valid_until, `${path}.valid_until`),
  };
}

function parseVersions(value: unknown, path: string): Record<string, string> {
  const o = obj(value, path);
  const out: Record<string, string> = {};
  for (const [k, v] of Object.entries(o)) out[k] = str(v, `${path}.${k}`, { nonEmpty: true });
  if (out.web_contract !== CONTRACT_VERSION) fail(`${path}.web_contract`, `${CONTRACT_VERSION}이 필요합니다`);
  return out;
}

const SUCCESS: readonly FinalStatus[] = ['optimal', 'feasible'];

/** final payload 검사. 상태와 결과 구조의 불변 조건이 맞지 않으면 성공 표시를 거부한다. */
export function parseFinal(value: unknown): FinalResult {
  const path = 'final';
  const o = obj(value, path);
  exactKeys(o, path, [
    'status',
    'conditions',
    'issues',
    'supported',
    'squad',
    'snapshot',
    'versions',
    'answer',
    'narration_source',
    'data_mode',
  ]);
  const status = oneOf(o.status, FINAL_STATUSES, `${path}.status`);
  const data_mode = oneOf(o.data_mode, DATA_MODES, `${path}.data_mode`);
  const conditions = o.conditions === null ? null : parseConditions(o.conditions, `${path}.conditions`);
  const issues = arr(o.issues, `${path}.issues`).map((v, i) => parseIssue(v, `${path}.issues[${i}]`));
  const snapshot = o.snapshot === null ? null : parseSnapshot(o.snapshot, `${path}.snapshot`);
  const answer = o.answer === null ? null : str(o.answer, `${path}.answer`);
  const narration_source = oneOf(o.narration_source, ['llm_checked', 'template', 'none'] as const, `${path}.narration_source`);

  if (SUCCESS.includes(status)) {
    if (conditions === null) fail(`${path}.conditions`, '성공 결과에는 정규화 조건이 필요합니다');
    if (o.squad === null) fail(`${path}.squad`, '성공 결과에는 스쿼드가 필요합니다');
    if (snapshot === null || snapshot.valid_until === null) fail(`${path}.snapshot`, '성공 결과에는 기한이 있는 스냅샷이 필요합니다');
    if (issues.length > 0) fail(`${path}.issues`, '성공 결과에는 조건 문제가 없어야 합니다');
  } else if (o.squad !== null) {
    fail(`${path}.squad`, `${status} 결과에는 스쿼드가 없어야 합니다`);
  }
  if (status === 'needs_clarification') {
    if (issues.length === 0) fail(`${path}.issues`, '되묻기 결과에는 사유가 필요합니다');
    if (conditions !== null) fail(`${path}.conditions`, '되묻기 결과에는 정규화 조건이 없어야 합니다');
  }
  if (narration_source !== 'none' && answer === null) fail(`${path}.answer`, '설명 출처가 있으면 설명이 필요합니다');
  if (narration_source === 'none' && answer !== null) fail(`${path}.answer`, '설명 출처가 none이면 설명이 없어야 합니다');

  return {
    status,
    conditions,
    issues,
    supported: parseSupported(o.supported, `${path}.supported`),
    squad: o.squad === null ? null : parseSquad(o.squad, `${path}.squad`, conditions!),
    snapshot,
    versions: parseVersions(o.versions, `${path}.versions`),
    answer,
    narration_source,
    data_mode,
  };
}

/**
 * SSE 한 이벤트(event 이름 + data 문자열)를 envelope로 해석한다.
 * 잘못된 JSON·버전·필드·이벤트 이름 불일치는 ContractError다.
 */
export function parseEnvelope(eventName: string, data: string): AgentEvent {
  return parseEnvelopeAs(eventName, data, CONTRACT_VERSION, parseFinal);
}

/** 계약 버전별 envelope 공통 해석. v2(webAgentV2.ts)가 같은 순서·필드 검사를 재사용한다. */
export type EnvelopeOf<F> =
  | { type: 'started'; seq: number; request_id: string; payload: StartedPayload }
  | { type: 'progress'; seq: number; request_id: string; payload: ProgressPayload }
  | { type: 'final'; seq: number; request_id: string; payload: F }
  | { type: 'error'; seq: number; request_id: string; payload: ErrorPayload };

export function parseEnvelopeAs<F>(
  eventName: string,
  data: string,
  version: string,
  finalParser: (payload: unknown) => F,
): EnvelopeOf<F> {
  let raw: unknown;
  try {
    raw = JSON.parse(data);
  } catch {
    throw new ContractError('event: JSON 형식이 아닙니다');
  }
  const o = obj(raw, 'event');
  exactKeys(o, 'event', ['contract_version', 'request_id', 'seq', 'type', 'payload']);
  if (o.contract_version !== version) fail('event.contract_version', `지원하지 않는 계약 버전입니다`);
  const type = oneOf(o.type, EVENT_TYPES, 'event.type');
  if (eventName !== type) fail('event', 'SSE 이벤트 이름과 type이 다릅니다');
  const request_id = str(o.request_id, 'event.request_id', { nonEmpty: true });
  const seq = int(o.seq, 'event.seq', 0, Number.MAX_SAFE_INTEGER);
  const p = obj(o.payload, 'event.payload');

  switch (type) {
    case 'started': {
      exactKeys(p, 'started', ['data_mode']);
      return { type, seq, request_id, payload: { data_mode: oneOf(p.data_mode, DATA_MODES, 'started.data_mode') } };
    }
    case 'progress': {
      exactKeys(p, 'progress', ['stage', 'message']);
      return {
        type,
        seq,
        request_id,
        payload: {
          stage: str(p.stage, 'progress.stage', { nonEmpty: true }),
          message: str(p.message, 'progress.message', { nonEmpty: true }),
        },
      };
    }
    case 'final':
      return { type, seq, request_id, payload: finalParser(p) };
    case 'error': {
      exactKeys(p, 'error', ['code', 'message']);
      return {
        type,
        seq,
        request_id,
        payload: {
          code: str(p.code, 'error.code', { nonEmpty: true }),
          message: str(p.message, 'error.message', { nonEmpty: true }),
        },
      };
    }
  }
}
