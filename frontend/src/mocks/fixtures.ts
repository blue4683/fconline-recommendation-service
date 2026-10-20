/**
 * 모의 응답 fixture. 모든 선수명·ID·수치는 가상이며 실존 선수/실제 수치·원본 자료와 무관하다.
 * 합계는 모의 "서버" 쪽에서 BigInt로 정확히 계산한다(브라우저 화면 로직과 분리).
 */
import {
  CONTRACT_VERSION,
  FORMATION_SLOTS,
  SUPPORTED_CLUBS,
  SUPPORTED_REINFORCEMENTS,
  type Conditions,
  type FinalResult,
  type FinalStatus,
  type ConditionIssue,
  type SlotCoverage,
  type SquadRow,
  type Supported,
} from '../contracts/webAgentV0';

export const SUPPORTED: Supported = {
  required_fields: ['price_cap', 'salary_cap', 'team_color', 'formation', 'allowed_reinforcements'],
  clubs: [...SUPPORTED_CLUBS],
  formations: Object.keys(FORMATION_SLOTS),
  reinforcements: [...SUPPORTED_REINFORCEMENTS],
};

const VERSIONS = {
  web_contract: CONTRACT_VERSION,
  normalizer: 'mock-normalizer',
  engine: 'mock-engine',
  model: 'unused',
};

/** 가상 이름. 실존 선수와 무관하게 지어낸 이름이다. */
const NAMES = [
  '하람 솔레',
  '윤새벽',
  '도마루 킴벨',
  '설지환',
  '편하늘',
  '기온유',
  '탁무진',
  '남궁별',
  '여울 바르크',
  '방채운',
  '석이든',
];

interface RowSeed {
  level: number;
  price: string;
  salary: string;
  ovr: number;
}

function rows(formation: string, seeds: RowSeed[], idBase: number, names = NAMES): SquadRow[] {
  const slots = FORMATION_SLOTS[formation]!;
  return slots.map((slot, i) => {
    const seed = seeds[i % seeds.length]!;
    return {
      slot,
      spid: String(900_000_000 + idBase + i),
      person_id: String(700_000 + idBase + i),
      name: names[i % names.length]!,
      reinforce_level: seed.level,
      price: seed.price,
      salary: seed.salary,
      raw_ovr: seed.ovr,
      performance: seed.ovr / 2,
      placement_type: 'base',
    };
  });
}

function squadOf(r: SquadRow[]) {
  return {
    rows: r,
    total_price: r.reduce((a, x) => a + BigInt(x.price), 0n).toString(),
    total_salary: r.reduce((a, x) => a + BigInt(x.salary), 0n).toString(),
    total_ovr: r.reduce((a, x) => a + x.raw_ovr, 0),
    performance: r.reduce((a, x) => a + x.raw_ovr, 0) / 2,
  };
}

const NORMAL_SEEDS: RowSeed[] = [
  { level: 7, price: '412000000000', salary: '18', ovr: 121 },
  { level: 6, price: '238500000000', salary: '17', ovr: 118 },
  { level: 5, price: '96400000000', salary: '15', ovr: 114 },
  { level: 8, price: '1120000000000', salary: '21', ovr: 127 },
  { level: 5, price: '87150000000', salary: '14', ovr: 112 },
  { level: 6, price: '301000000000', salary: '19', ovr: 119 },
  { level: 7, price: '655000000000', salary: '20', ovr: 124 },
  { level: 5, price: '143800000000', salary: '16', ovr: 116 },
  { level: 6, price: '274250000000', salary: '18', ovr: 120 },
  { level: 8, price: '1384000000000', salary: '22', ovr: 129 },
  { level: 7, price: '918600000000', salary: '23', ovr: 126 },
];

const HUGE_SEEDS: RowSeed[] = [
  // 34자리 BP 상한 근처와 2^53(9007199254740992) 초과 급여를 정확히 운반하는지 보기 위한 값.
  { level: 11, price: '900000000000000000000000000000001', salary: '900719925474099301', ovr: 150 },
  { level: 10, price: '12345678901234567890123456789012', salary: '9007199254740993', ovr: 148 },
  { level: 9, price: '3', salary: '1', ovr: 141 },
];

export type ScenarioId =
  | 'optimal'
  | 'feasible'
  | 'huge_numbers'
  | 'needs_clarification'
  | 'input_format'
  | 'infeasible'
  | 'unknown'
  | 'data_expired'
  | 'internal_error'
  | 'llm_unavailable'
  | 'expiring_soon'
  | 'server_error'
  | 'protocol_error'
  | 'disconnect';

export interface ScenarioInfo {
  id: ScenarioId;
  label: string;
}

/** 화면의 모의 시나리오 선택지. 원문을 해석하지 않으며 선택한 결과를 그대로 재생한다. */
export const SCENARIOS: ScenarioInfo[] = [
  { id: 'optimal', label: '최적 스쿼드' },
  { id: 'feasible', label: '가능 스쿼드 (최적성 미확인)' },
  { id: 'huge_numbers', label: '큰 금액 (34자리 BP·2^53 초과 급여)' },
  { id: 'expiring_soon', label: '곧 만료되는 결과 (30초)' },
  { id: 'needs_clarification', label: '조건 누락·미지원' },
  { id: 'input_format', label: '정형 조건문이 아닌 요청' },
  { id: 'infeasible', label: '해 없음' },
  { id: 'unknown', label: '시간 내 미확정' },
  { id: 'data_expired', label: '자료 만료' },
  { id: 'llm_unavailable', label: '모델 사용 불가' },
  { id: 'internal_error', label: '내부 오류' },
  { id: 'server_error', label: '스트림 오류 이벤트' },
  { id: 'protocol_error', label: '잘못된 이벤트 순서' },
  { id: 'disconnect', label: '결과 전 연결 끊김' },
];

const LIVERPOOL: Conditions = {
  price_cap: '6000000000000',
  salary_cap: '210',
  team_color: '리버풀',
  formation: '4-2-3-1',
  allowed_reinforcements: [5, 6, 7, 8],
};

const CHELSEA: Conditions = {
  price_cap: '5800000000000',
  salary_cap: '205',
  team_color: '첼시',
  formation: '4-1-2-3',
  allowed_reinforcements: [5, 6, 7, 8],
};

const MUNICH_HUGE: Conditions = {
  price_cap: '9999999999999999999999999999999999',
  salary_cap: '9223372036854775807',
  team_color: '바이에른 뮌헨',
  formation: '4-2-2-2',
  allowed_reinforcements: [9, 10, 11],
};

function base(status: FinalStatus): Omit<FinalResult, 'conditions' | 'squad' | 'snapshot' | 'answer' | 'narration_source'> {
  return { status, issues: [], supported: SUPPORTED, versions: { ...VERSIONS }, data_mode: 'synthetic' };
}

function snapshot(now: number, validForMs: number, id: string) {
  return {
    id,
    t0: new Date(now).toISOString(),
    valid_until: new Date(now + validForMs).toISOString(),
  };
}

const DAY = 24 * 60 * 60 * 1000;

const CLARIFY_ISSUES: ConditionIssue[] = [
  {
    field: 'salary_cap',
    kind: 'missing',
    code: 'missing_salary_cap',
    message: '급여 상한이 원문에 없습니다. 급여 상한을 숫자로 함께 적어 주세요.',
    occurrence: null,
  },
  {
    field: 'unsupported',
    kind: 'unsupported',
    code: 'unsupported_condition',
    message: '국적 조건은 아직 지원하지 않습니다. 해당 조건을 빼고 다시 요청해 주세요.',
    occurrence: 0,
  },
];

/**
 * 백엔드 후보 준비(prepare_candidates, develop 7c56bbc)가
 * '리버풀 4-2-3-1, 예산 6조 BP, 급여 210 이하, 5~8강으로 구성해줘'에 실제로 낸 사유를
 * WEB-003 제안 모양(kind·occurrence 추가, context_id 제외)으로 옮긴 것.
 */
const FORMAT_ISSUES: ConditionIssue[] = [
  {
    field: 'original',
    kind: 'invalid',
    code: 'unrecognized_context',
    message: '해석되지 않은 절이 있습니다. 정형 조건문으로 다시 입력해 주세요.',
    occurrence: 0,
    span: { start: 0, end: 11, text: '리버풀 4-2-3-1' },
  },
  {
    field: 'price_cap',
    kind: 'invalid',
    code: 'missing_coefficient',
    message: '단위 앞의 숫자를 명시해야 합니다.',
    occurrence: 0,
    span: { start: 16, end: 21, text: '6조 BP' },
  },
  {
    field: 'salary_cap',
    kind: 'invalid',
    code: 'invalid_salary',
    message: '급여는 올바른 쉼표를 쓰는 비음수 정수여야 합니다.',
    occurrence: 0,
    span: { start: 26, end: 32, text: '210 이하' },
  },
];

/** 포메이션 자리 순서의 후보 수. zero에 든 자리는 0으로 둔다. */
function coverageOf(formation: string, base: number, zero: readonly string[] = []): SlotCoverage[] {
  return FORMATION_SLOTS[formation]!.map((slot, i) => ({ slot, count: zero.includes(slot) ? 0 : base + ((i * 7) % 13) }));
}

/** 시나리오의 final payload. 'server_error'·'protocol_error'·'disconnect'는 final이 없다. */
export function finalFor(id: ScenarioId, now: number): FinalResult | null {
  switch (id) {
    case 'optimal': {
      const r = rows(LIVERPOOL.formation, NORMAL_SEEDS, 1000);
      return {
        ...base('optimal'),
        conditions: LIVERPOOL,
        squad: squadOf(r),
        snapshot: snapshot(now, DAY, 'mock-snap-optimal'),
        coverage: coverageOf(LIVERPOOL.formation, 24),
        evaluated_at: new Date(now).toISOString(),
        answer:
          '리버풀 4-2-3-1, 5~8강 조건에서 예산과 급여 상한을 지키는 조합 중 OVR 합이 가장 높은 스쿼드입니다. 수치는 표와 같습니다.',
        narration_source: 'llm_checked',
      };
    }
    case 'expiring_soon': {
      const r = rows(LIVERPOOL.formation, NORMAL_SEEDS, 1100);
      return {
        ...base('optimal'),
        conditions: LIVERPOOL,
        squad: squadOf(r),
        snapshot: snapshot(now, 30_000, 'mock-snap-expiring'),
        answer: '30초 뒤 만료되는 모의 결과입니다. 기한이 지나면 화면이 만료로 바뀌어야 합니다.',
        narration_source: 'template',
      };
    }
    case 'feasible': {
      const r = rows(CHELSEA.formation, [...NORMAL_SEEDS].reverse(), 2000);
      return {
        ...base('feasible'),
        conditions: CHELSEA,
        squad: squadOf(r),
        snapshot: snapshot(now, DAY, 'mock-snap-feasible'),
        coverage: coverageOf(CHELSEA.formation, 31),
        evaluated_at: new Date(now).toISOString(),
        answer: '조건을 모두 지키는 스쿼드를 찾았지만 제한 시간 안에 최적인지 확인하지 못했습니다.',
        narration_source: 'template',
      };
    }
    case 'huge_numbers': {
      const r = rows(MUNICH_HUGE.formation, HUGE_SEEDS, 3000);
      return {
        ...base('optimal'),
        conditions: MUNICH_HUGE,
        squad: squadOf(r),
        snapshot: snapshot(now, DAY, 'mock-snap-huge'),
        answer: null,
        narration_source: 'none',
      };
    }
    case 'needs_clarification':
      return {
        ...base('needs_clarification'),
        issues: CLARIFY_ISSUES,
        conditions: null,
        squad: null,
        snapshot: null,
        answer: '필수 조건 중 일부가 없거나 지원하지 않는 조건이 있어 추천을 시작하지 않았습니다.',
        narration_source: 'template',
      };
    case 'input_format':
      return {
        ...base('needs_clarification'),
        issues: FORMAT_ISSUES,
        conditions: null,
        squad: null,
        snapshot: null,
        answer: '해석하지 못한 조건 절이 있어 추천을 시작하지 않았습니다. 표시된 부분을 정형 조건문으로 고쳐 주세요.',
        narration_source: 'template',
      };
    case 'infeasible':
      return {
        ...base('infeasible'),
        conditions: { ...CHELSEA, price_cap: '150000000000' },
        squad: null,
        snapshot: snapshot(now, DAY, 'mock-snap-infeasible'),
        coverage: coverageOf(CHELSEA.formation, 3, ['LCB', 'RCB']),
        evaluated_at: new Date(now).toISOString(),
        answer: '보유한 합성 자료 안에서는 이 예산과 급여 상한을 함께 만족하는 11명을 찾지 못했습니다.',
        narration_source: 'template',
      };
    case 'unknown':
      return {
        ...base('unknown'),
        conditions: LIVERPOOL,
        squad: null,
        snapshot: snapshot(now, DAY, 'mock-snap-unknown'),
        coverage: coverageOf(LIVERPOOL.formation, 412),
        evaluated_at: new Date(now).toISOString(),
        answer: null,
        narration_source: 'none',
      };
    case 'data_expired':
      return {
        ...base('data_expired'),
        conditions: LIVERPOOL,
        squad: null,
        snapshot: null,
        answer: null,
        narration_source: 'none',
      };
    case 'internal_error':
      return {
        ...base('internal_error'),
        conditions: null,
        squad: null,
        snapshot: null,
        answer: null,
        narration_source: 'none',
        code: 'invalid_selection',
      };
    case 'llm_unavailable':
      return {
        ...base('llm_unavailable'),
        conditions: null,
        squad: null,
        snapshot: null,
        answer: null,
        narration_source: 'none',
        code: 'selection_provider_error',
      };
    case 'server_error':
    case 'protocol_error':
    case 'disconnect':
      return null;
  }
}

/** 테스트에서 이름 등을 바꾼 정상 결과를 만들 때 사용한다. */
export function optimalWithNames(names: string[], now: number): FinalResult {
  const r = rows(LIVERPOOL.formation, NORMAL_SEEDS, 5000, names);
  return {
    ...base('optimal'),
    conditions: LIVERPOOL,
    squad: squadOf(r),
    snapshot: snapshot(now, DAY, 'mock-snap-names'),
    answer: names[0] ?? null,
    narration_source: 'template',
  };
}
