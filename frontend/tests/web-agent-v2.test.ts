/**
 * web-agent.v2 계약: 방식 목록·v2 final·envelope·세션·409 처리.
 * 서버 기록은 generate_backend_v2_fixture.py가 실제 앱을 합성 자료·선택 대역으로 호출해 만든 것이다.
 * 실제 모델·네트워크·상주 서버 검증은 아니다.
 */
import { describe, expect, it, vi } from 'vitest';
import { parseObjectives } from '../src/contracts/agentObjectives';
import { ContractError, parseFinal } from '../src/contracts/webAgentV1';
import {
  isFinalV2,
  parseEnvelopeV2,
  parseFinalV2,
  scoreText,
  type FinalResultV2,
} from '../src/contracts/webAgentV2';
import { finalForV2, objectivesResponse, OBJECTIVE_LISTINGS, SCENARIOS, withObjective, finalFor } from '../src/mocks/fixtures';
import { createMockTransport } from '../src/mocks/mockTransport';
import { buildRequest, buildRequestV2, runStream } from '../src/services/agentSession';
import { createLiveTransport, PROPOSED_STREAM_PATH } from '../src/services/liveTransport';
import { createObjectivesChecker } from '../src/services/objectives';
import rawV1 from './fixtures/backend-agent-app.json';
import raw from './fixtures/backend-agent-v2.json';
import { envelopeV2, FIXED_NOW, listedObjectives, scriptedTransport, sse } from './helpers';

interface Stream {
  name: string;
  request_id: string;
  message: string;
  objective: 'ovr' | 'gk_priority_multi_factor';
  sse: string;
}
interface Recorded {
  name: string;
  status: number;
  body: Record<string, unknown>;
}
const fixture = raw as unknown as {
  backend_sha: string;
  fixture_data: string;
  policy_path: string;
  streams: Stream[];
  rejections: Recorded[];
  objectives: Recorded[];
};

const clone = <T>(v: T): T => JSON.parse(JSON.stringify(v)) as T;
const stream = (name: string) => fixture.streams.find((s) => s.name === name)!;
const recorded = (list: Recorded[], name: string) => list.find((r) => r.name === name)!;
const BASE = 'http://127.0.0.1:8001';

function finalPayload(s: { sse: string }): Record<string, unknown> {
  const line = s.sse.split('\n').find((l) => l.startsWith('data: ') && l.includes('"type":"final"'))!;
  return (JSON.parse(line.slice(6)) as { payload: Record<string, unknown> }).payload;
}

type Raw = {
  status: string;
  versions: Record<string, string>;
  objective: { id: string; policy: Record<string, unknown> | null; stages: Record<string, unknown>[] };
  squad: {
    rows: Record<string, unknown>[];
    outfield_ovr: number;
    gk_ovr: number;
    total_ovr: number;
    total_score_units: number | null;
    total_score: string | null;
  } & Record<string, unknown>;
} & Record<string, unknown>;

const multi = () => clone(finalPayload(stream('observed_multi_success'))) as Raw;
const ovr = () => clone(finalPayload(stream('observed_ovr_success'))) as Raw;

describe('v2 fixture 구성', () => {
  it('기준 백엔드·자료 출처·기록 이름이 기대와 같다', () => {
    expect(fixture.backend_sha).toMatch(/^[0-9a-f]{7,40}$/);
    expect(fixture.fixture_data).toBe('generated_synthetic_only_including_observed_mode');
    expect(fixture.streams.map((s) => s.name)).toEqual([
      'synthetic_ovr_success',
      'observed_ovr_success',
      'observed_multi_success',
      'observed_multi_chelsea',
      'observed_multi_infeasible',
      'observed_multi_missing_budget',
      'observed_ovr_infeasible',
      'observed_ovr_missing_budget',
      'observed_ovr_conflict',
      'observed_multi_provider_error',
    ]);
    expect(fixture.rejections.map((r) => [r.name, r.status])).toEqual([
      ['objective_unavailable', 409],
      ['objective_not_configured', 409],
      ['v2_without_objective', 422],
      ['v1_with_objective', 422],
      ['v2_unknown_objective', 422],
    ]);
    expect(fixture.objectives.map((o) => [o.name, o.status])).toEqual([
      ['observed_available', 200],
      ['observed_not_configured', 200],
      ['observed_policy_mismatch', 200],
      ['synthetic_material', 200],
      ['agent_unavailable', 503],
    ]);
  });
});

describe('scoreText', () => {
  it.each([
    [0, '0.00'],
    [5, '0.05'],
    [70, '0.70'],
    [7085, '70.85'],
    [10000, '100.00'],
    [110000, '1100.00'],
  ])('%i → %s (정수 연산, 반올림 없음)', (units, text) => {
    expect(scoreText(units)).toBe(text);
  });
});

describe('parseFinalV2 — 서버 기록', () => {
  it.each(fixture.streams.filter((s) => !s.name.endsWith('provider_error')).map((s) => [s.name, s] as const))(
    '%s final이 v2 계약을 통과한다',
    (_name, s) => {
      const result = parseFinalV2(clone(finalPayload(s)));
      expect(result.versions.web_contract).toBe('web-agent.v2');
      expect(result.objective.id).toBe(s.objective);
      expect(isFinalV2(result)).toBe(true);
    },
  );

  it('다요소 성공은 정책·세 단계·서버 점수 문자열을 그대로 담는다', () => {
    const payload = multi();
    const result = parseFinalV2(clone(payload));
    expect(result.objective.policy).toEqual(payload.objective.policy);
    expect(result.objective.policy!.status).toBe('experimental');
    expect(result.objective.stages.map((s) => s.name)).toEqual(['outfield_ovr', 'gk_ovr', 'multi_factor']);
    const squad = result.squad!;
    expect(squad.total_score).toBe(payload.squad.total_score);
    expect(squad.rows.map((r) => r.score)).toEqual(payload.squad.rows.map((r) => r.score));
    expect(squad.outfield_ovr + squad.gk_ovr).toBe(squad.total_ovr);
    // 기존 OVR/2 성능 필드에 다요소 점수를 넣지 않는다.
    for (const r of squad.rows) expect(r.performance).toBe(r.raw_ovr / 2);
  });

  it('OVR 방식은 점수·정책·단계가 없고 필드/GK OVR만 더한다', () => {
    const result = parseFinalV2(ovr());
    expect(result.objective).toEqual({ id: 'ovr', policy: null, stages: [] });
    expect(result.squad!.total_score_units).toBeNull();
    expect(result.squad!.rows.every((r) => r.score === null && r.score_units === null)).toBe(true);
  });

  it('다요소 비성공은 정책만 있고 단계·표가 없다', () => {
    const result = parseFinalV2(clone(finalPayload(stream('observed_multi_infeasible'))));
    expect(result.status).toBe('infeasible');
    expect(result.objective.policy).not.toBeNull();
    expect(result.objective.stages).toEqual([]);
    expect(result.squad).toBeNull();
  });

  it('v2 payload는 v1 파서가 받지 않는다(모르는 필드·버전)', () => {
    expect(() => parseFinal(multi())).toThrow(ContractError);
  });
});

describe('parseFinalV2 — 변조 거부', () => {
  const cases: [string, () => Raw, (d: Raw) => void][] = [
    ['행 점수 표기 불일치', multi, (d) => (d.squad.rows[0]!.score = '0.00')],
    ['행 점수 한 자리 소수', multi, (d) => (d.squad.rows[0]!.score = (d.squad.rows[0]!.score as string).slice(0, -1))],
    ['행 점수 숫자형', multi, (d) => (d.squad.rows[0]!.score = Number(d.squad.rows[0]!.score))],
    ['정수 점수 소수', multi, (d) => (d.squad.rows[0]!.score_units = 48.75)],
    ['정수 점수 상한 초과', multi, (d) => {
      d.squad.rows[0]!.score_units = 10001;
      d.squad.rows[0]!.score = '100.01';
    }],
    ['합계 정수 +1', multi, (d) => (d.squad.total_score_units = d.squad.total_score_units! + 1)],
    ['합계 표기 불일치', multi, (d) => (d.squad.total_score = '0.00')],
    ['GK OVR +1', multi, (d) => (d.squad.gk_ovr += 1)],
    ['필드 OVR +1', multi, (d) => (d.squad.outfield_ovr += 1)],
    ['다요소 성공에 단계 없음', multi, (d) => (d.objective.stages = [])],
    ['단계 순서 바뀜', multi, (d) => d.objective.stages.reverse()],
    ['단계 이름 중복', multi, (d) => (d.objective.stages[1] = { ...d.objective.stages[0]! })],
    ['모르는 단계 상태', multi, (d) => (d.objective.stages[0]!.status = 'proven')],
    ['다요소에 정책 없음', multi, (d) => (d.objective.policy = null)],
    ['정책 해시 형식', multi, (d) => (d.objective.policy!.version = 'abc')],
    ['정책 상태', multi, (d) => (d.objective.policy!.status = 'stable')],
    ['다요소 행 점수 누락', multi, (d) => Object.assign(d.squad.rows[0]!, { score_units: null, score: null })],
    ['웹 계약 v1 표기', multi, (d) => (d.versions.web_contract = 'web-agent.v1')],
    ['objective 누락', multi, (d) => delete (d as Record<string, unknown>).objective],
    ['squad v2 필드 누락', multi, (d) => delete (d.squad as Record<string, unknown>).outfield_ovr],
    ['행 v2 필드 누락', multi, (d) => delete d.squad.rows[0]!.score],
    ['objective 모르는 필드', multi, (d) => ((d.objective as Record<string, unknown>).weights = {})],
    ['단계 모르는 필드', multi, (d) => (d.objective.stages[0]!.note = 'x')],
    ['squad 모르는 필드', multi, (d) => (d.squad.bonus = 1)],
    ['OVR에 정책', ovr, (d) => (d.objective.policy = multi().objective.policy)],
    ['OVR에 단계', ovr, (d) => (d.objective.stages = multi().objective.stages)],
    ['OVR 행에 점수', ovr, (d) => Object.assign(d.squad.rows[0]!, { score_units: 5000, score: '50.00' })],
    ['OVR에 합계 점수', ovr, (d) => Object.assign(d.squad, { total_score_units: 0, total_score: '0.00' })],
    ['방식 id 바뀜', ovr, (d) => (d.objective.id = 'gk_priority_multi_factor')],
    ['모르는 방식', ovr, (d) => (d.objective.id = 'best')],
  ];

  it.each(cases)('%s → 거부', (_name, make, change) => {
    const data = make();
    expect(() => parseFinalV2(clone(data))).not.toThrow();
    change(data);
    expect(() => parseFinalV2(data)).toThrow(ContractError);
  });

  it('비성공 결과에 단계가 있으면 거부한다', () => {
    const data = clone(finalPayload(stream('observed_multi_infeasible'))) as Raw;
    data.objective.stages = multi().objective.stages;
    expect(() => parseFinalV2(data)).toThrow(/단계 요약/);
  });
});

describe('모의 v2 자료', () => {
  it('모든 final 시나리오 × 두 방식이 v2 계약을 통과한다', () => {
    const ids = SCENARIOS.map((s) => s.id).filter((id) => finalForV2(id, FIXED_NOW, 'ovr') !== null);
    expect(ids.length).toBeGreaterThanOrEqual(9);
    for (const id of ids) {
      for (const objective of ['ovr', 'gk_priority_multi_factor'] as const) {
        expect(() => parseFinalV2(clone(finalForV2(id, FIXED_NOW, objective))), `${id}/${objective}`).not.toThrow();
      }
    }
  });

  it('모의 다요소 가능해는 미확정·미실행 단계를 담는다', () => {
    const result = parseFinalV2(clone(finalForV2('feasible', FIXED_NOW, 'gk_priority_multi_factor')));
    expect(result.objective.stages.map((s) => s.status)).toEqual(['optimal', 'incomplete', 'not_run']);
    expect(result.objective.stages[2]!.value).toBeNull();
  });

  it('모의 방식 목록 네 가지가 실제 파서를 통과한다', () => {
    for (const { id } of OBJECTIVE_LISTINGS) {
      const r = objectivesResponse(id);
      const listing = parseObjectives(r.body, r.status);
      expect(listing.objectives[1]!.available, id).toBe(id === 'available');
    }
  });
});

describe('parseEnvelopeV2', () => {
  it('v2 envelope만 받는다', () => {
    const payload = finalForV2('optimal', FIXED_NOW, 'ovr');
    expect(parseEnvelopeV2('final', envelopeV2('r', 1, 'final', payload)).type).toBe('final');
    const v1 = JSON.stringify({ contract_version: 'web-agent.v1', request_id: 'r', seq: 1, type: 'final', payload });
    expect(() => parseEnvelopeV2('final', v1)).toThrow(/계약 버전/);
  });
});

describe('세션 — v2 요청', () => {
  it.each(fixture.streams.map((s) => [s.name, s] as const))('%s 응답 바이트를 1·7바이트 청크로 그대로 통과한다', async (name, s) => {
    for (const chunk of [1, 7]) {
      const outcome = await runStream(
        scriptedTransport(() => s.sse, chunk),
        buildRequestV2(s.message, s.objective, s.request_id),
        new AbortController().signal,
      );
      if (name.endsWith('provider_error')) {
        expect(outcome).toMatchObject({ kind: 'server_error', code: 'selection_provider_error' });
      } else {
        expect(outcome.kind).toBe('final');
        if (outcome.kind === 'final') expect(outcome.result).toEqual(parseFinalV2(finalPayload(s)));
      }
    }
  });

  it('v2 요청에 v1 envelope가 오면 프로토콜 오류다', async () => {
    const v1 = (rawV1 as unknown as { streams: { name: string; request_id: string; sse: string }[] }).streams[0]!;
    const outcome = await runStream(
      scriptedTransport(() => v1.sse),
      buildRequestV2('요청', 'ovr', v1.request_id),
      new AbortController().signal,
    );
    expect(outcome.kind).toBe('protocol_error');
  });

  it('v1 요청에 v2 envelope가 오면 프로토콜 오류다', async () => {
    const s = stream('observed_ovr_success');
    const outcome = await runStream(scriptedTransport(() => s.sse), buildRequest(s.message, s.request_id), new AbortController().signal);
    expect(outcome.kind).toBe('protocol_error');
  });

  it('요청한 방식과 다른 방식의 결과는 보이지 않는다', async () => {
    const s = stream('observed_multi_success');
    const outcome = await runStream(scriptedTransport(() => s.sse), buildRequestV2(s.message, 'ovr', s.request_id), new AbortController().signal);
    expect(outcome).toMatchObject({ kind: 'protocol_error' });
    if (outcome.kind === 'protocol_error') expect(outcome.detail).toContain('요청한 방식');
  });

  it('buildRequestV2는 원문을 그대로 두고 방식을 함께 싣는다', async () => {
    const message = '  예산 6조,\t리버풀 😀 ';
    expect(buildRequestV2(message, 'gk_priority_multi_factor', 'rid')).toEqual({
      contract_version: 'web-agent.v2',
      request_id: 'rid',
      message,
      objective: 'gk_priority_multi_factor',
    });
    const fetchImpl = vi.fn(async (_u: string, _i: RequestInit) => new Response('', { status: 200, headers: { 'content-type': 'text/event-stream' } }));
    await createLiveTransport(BASE, fetchImpl).open(buildRequestV2(message, 'ovr', 'rid'), new AbortController().signal);
    const [url, init] = fetchImpl.mock.calls[0]!;
    expect(url).toBe(`${BASE}${PROPOSED_STREAM_PATH}`);
    expect(JSON.parse(init.body as string)).toEqual({ contract_version: 'web-agent.v2', request_id: 'rid', message, objective: 'ovr' });
  });

  it('409 objective_unavailable은 코드가 있는 통신 오류이며 다시 보내거나 OVR로 바꾸지 않는다', async () => {
    const r = recorded(fixture.rejections, 'objective_unavailable');
    const fetchImpl = vi.fn(async () => new Response(JSON.stringify(r.body), { status: r.status, headers: { 'content-type': 'application/json' } }));
    const outcome = await runStream(
      createLiveTransport(BASE, fetchImpl),
      buildRequestV2('요청', 'gk_priority_multi_factor'),
      new AbortController().signal,
    );
    expect(outcome).toEqual(expect.objectContaining({ kind: 'transport_error', status: 409, code: 'objective_unavailable' }));
    expect(fetchImpl).toHaveBeenCalledTimes(1);
  });

  it('409 본문을 해석하지 못하면 코드 없이 상태만 전달한다', async () => {
    for (const body of ['not json', '{"detail":"busy"}', '{"detail":{"code":"<b>x</b>"}}']) {
      const fetchImpl = vi.fn(async () => new Response(body, { status: 409 }));
      const outcome = await runStream(createLiveTransport(BASE, fetchImpl), buildRequestV2('요청', 'ovr'), new AbortController().signal);
      expect(outcome).toEqual({ kind: 'transport_error', status: 409, detail: expect.any(String) });
    }
  });

  it('모의 transport는 목록에서 사용할 수 없는 방식을 409로 거절한다', async () => {
    const check = listedObjectives('not_configured');
    const listing = check.kind === 'listed' ? check.listing : null;
    const open = createMockTransport({ scenario: () => 'optimal', now: () => FIXED_NOW, listing: () => listing });
    const outcome = await runStream(open, buildRequestV2('요청', 'gk_priority_multi_factor'), new AbortController().signal);
    expect(outcome).toMatchObject({ kind: 'transport_error', status: 409, code: 'objective_unavailable' });
    const ok = await runStream(open, buildRequestV2('요청', 'ovr'), new AbortController().signal);
    expect(ok.kind).toBe('final');
  });

  it('모의 transport는 v1 요청에는 v1, v2 요청에는 v2로 응답한다', async () => {
    const mock = createMockTransport({ scenario: () => 'optimal', now: () => FIXED_NOW });
    const v1 = await runStream(mock, buildRequest('요청'), new AbortController().signal);
    const v2 = await runStream(mock, buildRequestV2('요청', 'gk_priority_multi_factor'), new AbortController().signal);
    expect(v1.kind === 'final' && isFinalV2(v1.result)).toBe(false);
    expect(v2.kind === 'final' && isFinalV2(v2.result) && v2.result.objective.id).toBe('gk_priority_multi_factor');
  });
});

describe('GET /objectives', () => {
  it.each(fixture.objectives.map((o) => [o.name, o] as const))('%s 본문을 상태 코드와 함께 해석한다', (name, o) => {
    const listing = parseObjectives(clone(o.body), o.status);
    expect(listing.default).toBe('ovr');
    const [ovrOption, multiOption] = listing.objectives;
    expect(ovrOption!.available).toBe(name !== 'agent_unavailable');
    expect(multiOption!.available).toBe(name === 'observed_available');
    if (name !== 'observed_available') expect(multiOption!.reason).toBe(name.replace('observed_', ''));
    expect(multiOption!.parameters).toEqual({ gk_exchange: 15, outfield_tolerance: 5, gk_tolerance: 2, search_seconds: 30 });
  });

  const base = () => clone(recorded(fixture.objectives, 'observed_available').body) as { objectives: Record<string, unknown>[] } & Record<string, unknown>;
  const tamper: [string, (d: ReturnType<typeof base>) => void, number?][] = [
    ['스키마 버전', (d) => (d.schema_version = 'agent-objectives.v2')],
    ['기본 방식 변경', (d) => (d.default = 'gk_priority_multi_factor')],
    ['모르는 최상위 필드', (d) => (d.extra = 1)],
    ['방식 중복', (d) => (d.objectives[1] = clone(d.objectives[0]!))],
    ['방식 누락', (d) => d.objectives.pop()],
    ['모르는 방식', (d) => (d.objectives[1]!.id = 'best')],
    ['사용 가능인데 사유', (d) => (d.objectives[1]!.reason = 'not_configured')],
    ['사용 불가인데 사유 없음', (d) => (d.objectives[1]!.available = false)],
    ['사용 불가인데 정책', (d) => Object.assign(d.objectives[1]!, { available: false, reason: 'not_configured' })],
    ['사용 가능인데 정책 없음', (d) => (d.objectives[1]!.policy = null)],
    ['다요소 설정 없음', (d) => (d.objectives[1]!.parameters = null)],
    ['OVR에 정책', (d) => (d.objectives[0]!.policy = clone(d.objectives[1]!.policy))],
    ['OVR이 다요소 사유로 막힘', (d) => Object.assign(d.objectives[0]!, { available: false, reason: 'not_configured' })],
    ['모르는 사유', (d) => Object.assign(d.objectives[1]!, { available: false, reason: 'busy', policy: null })],
    ['available 문자열', (d) => (d.objectives[0]!.available = 'true')],
    ['탐색 시간 문자열', (d) => ((d.objectives[1]!.parameters as Record<string, unknown>).search_seconds = '30')],
    ['모르는 설정 필드', (d) => ((d.objectives[1]!.parameters as Record<string, unknown>).weights = 1)],
    ['정상 목록인데 503', () => undefined, 503],
  ];
  it.each(tamper)('%s → 거부', (_name, change, status = 200) => {
    const d = base();
    expect(() => parseObjectives(clone(d), 200)).not.toThrow();
    change(d);
    expect(() => parseObjectives(d, status)).toThrow(ContractError);
  });

  it('서버 준비 실패 목록은 503에서만 받는다', () => {
    const down = clone(recorded(fixture.objectives, 'agent_unavailable').body);
    expect(() => parseObjectives(down, 200)).toThrow(ContractError);
  });

  it('GET 한 번이며 연결 실패·예상 밖 상태·형식 오류를 구분한다', async () => {
    const r = recorded(fixture.objectives, 'observed_available');
    const ok = vi.fn(async (_u: string, _i: RequestInit) => new Response(JSON.stringify(r.body), { status: 200 }));
    expect((await createObjectivesChecker(ok)(BASE)).kind).toBe('listed');
    const [url, init] = ok.mock.calls[0]!;
    expect(url).toBe(`${BASE}/objectives`);
    expect(init).toMatchObject({ method: 'GET', credentials: 'omit', cache: 'no-store', redirect: 'error' });
    expect(ok).toHaveBeenCalledTimes(1);
    expect(await createObjectivesChecker(async () => Promise.reject(new TypeError('x')))(BASE)).toEqual({ kind: 'unreachable' });
    // 이 경로가 없는 이전 서버는 404이며 방식을 추측하지 않는다.
    expect((await createObjectivesChecker(async () => new Response('{}', { status: 404 }))(BASE)).kind).toBe('invalid');
    expect((await createObjectivesChecker(async () => new Response('nope', { status: 200 }))(BASE)).kind).toBe('invalid');
  });
});

describe('withObjective', () => {
  it('v1 모의 결과의 수치·이름을 바꾸지 않는다', () => {
    const v1 = finalFor('optimal', FIXED_NOW)!;
    const v2: FinalResultV2 = withObjective(v1, 'gk_priority_multi_factor');
    expect(v2.squad!.rows.map((r) => [r.name, r.price, r.raw_ovr])).toEqual(v1.squad!.rows.map((r) => [r.name, r.price, r.raw_ovr]));
    expect(v2.squad!.total_price).toBe(v1.squad!.total_price);
  });
});

describe('envelope helper', () => {
  it('sse 도우미는 v2 이벤트도 같은 형식으로 만든다', () => {
    expect(sse('started', envelopeV2('r', 0, 'started', { data_mode: 'synthetic' }))).toContain('"contract_version":"web-agent.v2"');
  });
});
