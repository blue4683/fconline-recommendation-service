import { describe, expect, it } from 'vitest';
import { ContractError, parseEnvelope, parseFinal, type FinalResult } from '../src/contracts/webAgentV0';
import { finalFor, SCENARIOS, type ScenarioId } from '../src/mocks/fixtures';
import { envelope, FIXED_NOW } from './helpers';

function clone<T>(v: T): T {
  return JSON.parse(JSON.stringify(v)) as T;
}

function optimal(): FinalResult {
  return clone(finalFor('optimal', FIXED_NOW)!);
}

describe('parseFinal', () => {
  it('모든 final 시나리오 fixture가 계약을 통과한다', () => {
    const ids = SCENARIOS.map((s) => s.id).filter((id) => finalFor(id, FIXED_NOW) !== null);
    expect(ids.length).toBeGreaterThanOrEqual(9);
    for (const id of ids as ScenarioId[]) {
      const raw = clone(finalFor(id, FIXED_NOW));
      expect(() => parseFinal(raw), id).not.toThrow();
    }
  });

  it('34자리 BP와 2^53 초과 급여를 문자열 그대로 보존한다', () => {
    const result = parseFinal(clone(finalFor('huge_numbers', FIXED_NOW)));
    expect(result.conditions!.price_cap).toBe('9999999999999999999999999999999999');
    expect(result.conditions!.salary_cap).toBe('9223372036854775807');
    expect(result.squad!.rows[0]!.price).toBe('900000000000000000000000000000001');
    expect(result.squad!.rows[1]!.salary).toBe('9007199254740993');
    expect(typeof result.squad!.total_price).toBe('string');
  });

  it('금액을 JSON number로 보내면 거부한다', () => {
    const raw = optimal() as unknown as { squad: { rows: { price: unknown }[] } };
    raw.squad.rows[0]!.price = 412000000000;
    expect(() => parseFinal(raw)).toThrow(ContractError);
  });

  it('35자리 BP·급여 2^63 이상·부호·소수·선행 0을 거부한다', () => {
    for (const bad of ['10000000000000000000000000000000000', '-1', '1.5', '01', ' 1', '1e3']) {
      const raw = optimal();
      raw.conditions!.price_cap = bad;
      expect(() => parseFinal(raw), bad).toThrow(ContractError);
    }
    const raw = optimal();
    raw.conditions!.salary_cap = '9223372036854775808';
    expect(() => parseFinal(raw)).toThrow(/허용 범위/);
  });

  it('합계가 행 합과 다르면 보정하지 않고 거부한다', () => {
    const raw = optimal();
    raw.squad!.total_price = (BigInt(raw.squad!.total_price) + 1n).toString();
    expect(() => parseFinal(raw)).toThrow(/행 합계/);
  });

  it('예산·급여 상한을 넘는 성공 결과를 거부한다', () => {
    const raw = optimal();
    raw.conditions!.salary_cap = '100';
    expect(() => parseFinal(raw)).toThrow(/급여 상한/);
  });

  it('11명이 아니거나 포메이션 자리·인물 중복·허용 밖 강화면 거부한다', () => {
    const short = optimal();
    short.squad!.rows.pop();
    expect(() => parseFinal(short)).toThrow(ContractError);

    const dupPerson = optimal();
    dupPerson.squad!.rows[1]!.person_id = dupPerson.squad!.rows[0]!.person_id;
    expect(() => parseFinal(dupPerson)).toThrow(/인물/);

    const slot = optimal();
    slot.squad!.rows[0]!.slot = 'CF';
    expect(() => parseFinal(slot)).toThrow(/포메이션 자리/);

    const level = optimal();
    level.squad!.rows[0]!.reinforce_level = 11;
    expect(() => parseFinal(level)).toThrow(/허용 강화/);
  });

  it('성공에는 스쿼드와 기한이 있는 스냅샷이 필요하다', () => {
    const noSquad = optimal();
    noSquad.squad = null;
    expect(() => parseFinal(noSquad)).toThrow(/스쿼드가 필요/);

    const noDeadline = optimal();
    noDeadline.snapshot!.valid_until = null;
    expect(() => parseFinal(noDeadline)).toThrow(/기한/);

    const naive = optimal();
    naive.snapshot!.valid_until = '2026-10-21T10:00:00';
    expect(() => parseFinal(naive)).toThrow(/시간대/);
  });

  it('실패 상태에 스쿼드가 실려 오면 거부한다', () => {
    for (const status of ['infeasible', 'unknown', 'data_expired', 'internal_error', 'needs_clarification'] as const) {
      const raw = optimal();
      raw.status = status;
      expect(() => parseFinal(raw), status).toThrow(ContractError);
    }
  });

  it('되묻기 결과에는 사유가 있어야 하고 정규화 조건이 없어야 한다', () => {
    const raw = clone(finalFor('needs_clarification', FIXED_NOW)!);
    raw.issues = [];
    expect(() => parseFinal(raw)).toThrow(/사유/);
  });

  it('알 수 없는 필드·data_mode·계약 버전 불일치를 거부한다', () => {
    const extra = optimal() as unknown as Record<string, unknown>;
    extra.validated = true;
    expect(() => parseFinal(extra)).toThrow(/알 수 없는 필드/);

    const mode = optimal() as unknown as Record<string, unknown>;
    mode.data_mode = 'real';
    expect(() => parseFinal(mode)).toThrow(/synthetic/);

    const ver = optimal();
    ver.versions.web_contract = 'web-agent.v1';
    expect(() => parseFinal(ver)).toThrow(/web-agent.v0/);
  });
});

describe('parseFinal — 백엔드 내부 필드', () => {
  it('v0에 없는 내부 필드(code·coverage·evaluated_at·kind 등)는 최상위에서 거부한다', () => {
    for (const key of ['code', 'coverage', 'evaluated_at', 'kind', 'presentation_version', 'model_metadata']) {
      const raw = clone(finalFor('infeasible', FIXED_NOW)!) as unknown as Record<string, unknown>;
      raw[key] = null;
      expect(() => parseFinal(raw), key).toThrow(/알 수 없는 필드/);
    }
  });

  it('사유의 원문 구간·문맥 ID는 v0 issue에서 거부한다', () => {
    for (const key of ['span', 'context_id']) {
      const raw = clone(finalFor('input_format', FIXED_NOW)!);
      (raw.issues[0] as unknown as Record<string, unknown>)[key] = null;
      expect(() => parseFinal(raw), key).toThrow(/알 수 없는 필드/);
    }
  });

  it('준비 단계 사유(prepare.* 코드·occurrence null·field original)를 그대로 받는다', () => {
    const result = parseFinal(clone(finalFor('input_format', FIXED_NOW)));
    expect(result.issues.map((i) => i.code)).toEqual([
      'prepare.unrecognized_context',
      'prepare.missing_coefficient',
      'prepare.invalid_salary',
    ]);
    expect(result.issues.every((i) => i.occurrence === null)).toBe(true);
    expect(result.issues[0]!.field).toBe('original');
  });
});

describe('parseEnvelope', () => {
  it('SSE 이벤트 이름과 type이 다르면 거부한다', () => {
    const data = envelope('r', 0, 'started', { data_mode: 'synthetic' });
    expect(() => parseEnvelope('progress', data)).toThrow(/이벤트 이름/);
  });

  it('잘못된 JSON·모르는 버전·모르는 type을 거부한다', () => {
    expect(() => parseEnvelope('started', '{oops')).toThrow(/JSON/);
    expect(() =>
      parseEnvelope('started', envelope('r', 0, 'started', { data_mode: 'synthetic' }, { contract_version: 'web-agent.v9' })),
    ).toThrow(/계약 버전/);
    expect(() => parseEnvelope('token', envelope('r', 0, 'token', {}))).toThrow(/허용 값/);
  });
});
