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
  type SquadRow,
  type Supported,
} from '../contracts/webAgentV0';

export const SUPPORTED: Supported = {
  required_fields: ['price_cap', 'salary_cap', 'team_color', 'formation', 'allowed_reinforcements'],
  clubs: [...SUPPORTED_CLUBS],
  formations: Object.keys(FORMATION_SLOTS),
  reinforcements: [...SUPPORTED_REINFORCEMENTS],
};

/** 백엔드 W4-1 투영(develop 4ff544d)과 같은 키. 값은 모의 표기이며 실제 해시가 아니다. */
const VERSIONS: Record<string, string> = {
  web_contract: CONTRACT_VERSION,
  projection: 'mock-projection',
  preparation: 'mock-preparation',
  selection: 'mock-selection',
  adapter: 'mock-adapter',
  normalizer: 'mock-normalizer',
  graph: 'mock-graph',
  engine: 'mock-engine',
  presentation: 'mock-presentation',
  material: 'mock-material',
  model: 'unused',
  model_digest: 'unused',
  selector: 'unused',
};

/** 준비 단계에서 막혀 선택·정규화·엔진을 실행하지 않은 경우의 versions. */
const PREPARE_VERSIONS: Record<string, string> = {
  ...VERSIONS,
  selection: 'unused',
  normalizer: 'unused',
  graph: 'unused',
  engine: 'unused',
  presentation: 'unused',
  material: 'unused',
};

/** 백엔드 템플릿 안내(FRS/api/agent_projection.py CLARIFY). */
const CLARIFY_ANSWER = '필수 조건과 안내된 사유를 확인하고 완전한 요청을 다시 입력해 주세요.';

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
  | 'expiring_soon'
  | 'selection_error'
  | 'request_deadline'
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
  { id: 'needs_clarification', label: '조건 누락 (급여 상한 없음)' },
  { id: 'input_format', label: '정형 조건문이 아닌 요청' },
  { id: 'infeasible', label: '해 없음' },
  { id: 'unknown', label: '시간 내 미확정' },
  { id: 'data_expired', label: '자료 만료' },
  { id: 'internal_error', label: '내부 오류' },
  { id: 'selection_error', label: '조건 추출 모델 사용 불가' },
  { id: 'request_deadline', label: '응답 기한 초과' },
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

/** 선택 뒤 정규화가 급여 상한 누락을 찾은 경우. 백엔드 정규화 사유를 그대로 옮겼다. */
const CLARIFY_ISSUES: ConditionIssue[] = [
  {
    field: 'salary_cap',
    kind: 'missing',
    code: 'missing_condition',
    message: '필수 조건을 원문에 명시해야 합니다.',
    occurrence: null,
  },
];

/**
 * 준비 단계 사유. 백엔드 투영은 code에 prepare. 접두어를 붙이고 occurrence는 null이며 원문 구간은 보내지 않는다.
 * '리버풀 4-2-3-1, 예산 6조 BP, 급여 210 이하, 5~8강으로 구성해줘'의 실제 투영 결과와 같다.
 */
const FORMAT_ISSUES: ConditionIssue[] = [
  {
    field: 'original',
    kind: 'invalid',
    code: 'prepare.unrecognized_context',
    message: '해석되지 않은 절이 있습니다. 정형 조건문으로 다시 입력해 주세요.',
    occurrence: null,
  },
  {
    field: 'price_cap',
    kind: 'invalid',
    code: 'prepare.missing_coefficient',
    message: '단위 앞의 숫자를 명시해야 합니다.',
    occurrence: null,
  },
  {
    field: 'salary_cap',
    kind: 'invalid',
    code: 'prepare.invalid_salary',
    message: '급여는 올바른 쉼표를 쓰는 비음수 정수여야 합니다.',
    occurrence: null,
  },
];

/** 시나리오의 final payload. 'selection_error'·'request_deadline'·'protocol_error'·'disconnect'는 final이 없다. */
export function finalFor(id: ScenarioId, now: number): FinalResult | null {
  switch (id) {
    case 'optimal': {
      const r = rows(LIVERPOOL.formation, NORMAL_SEEDS, 1000);
      return {
        ...base('optimal'),
        conditions: LIVERPOOL,
        squad: squadOf(r),
        snapshot: snapshot(now, DAY, 'mock-snap-optimal'),
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
        versions: { ...VERSIONS, engine: 'unused', material: 'unused' },
        conditions: null,
        squad: null,
        snapshot: null,
        answer: CLARIFY_ANSWER,
        narration_source: 'template',
      };
    case 'input_format':
      return {
        ...base('needs_clarification'),
        issues: FORMAT_ISSUES,
        versions: { ...PREPARE_VERSIONS },
        conditions: null,
        squad: null,
        snapshot: null,
        answer: CLARIFY_ANSWER,
        narration_source: 'template',
      };
    case 'infeasible':
      return {
        ...base('infeasible'),
        conditions: { ...CHELSEA, price_cap: '150000000000' },
        squad: null,
        snapshot: snapshot(now, DAY, 'mock-snap-infeasible'),
        answer: '보유한 합성 자료 안에서는 이 예산과 급여 상한을 함께 만족하는 11명을 찾지 못했습니다.',
        narration_source: 'template',
      };
    case 'unknown':
      return {
        ...base('unknown'),
        conditions: LIVERPOOL,
        squad: null,
        snapshot: snapshot(now, DAY, 'mock-snap-unknown'),
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
      };
    case 'selection_error':
    case 'request_deadline':
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
