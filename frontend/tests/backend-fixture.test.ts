/**
 * 실제 8001 합성 Agent 앱(develop c0c47be)을 TestClient로 호출해 기록한 fixture를 웹 파서·세션·화면에 통과시킨다.
 * 다시 만들 때는 tests/fixtures/generate_backend_fixture.py. 상주 서버·실제 모델 연결 검증은 아니다.
 */
import { flushPromises, mount } from '@vue/test-utils';
import { afterEach, describe, expect, it, vi } from 'vitest';
import App from '../src/App.vue';
import { parseHealth } from '../src/contracts/agentHealth';
import { ContractError, parseFinal } from '../src/contracts/webAgentV0';
import { buildRequest, runStream } from '../src/services/agentSession';
import { createHealthChecker, type HealthCheck } from '../src/services/health';
import { createLiveTransport } from '../src/services/liveTransport';
import { groupDigits } from '../src/utils/format';
import raw from './fixtures/backend-agent-app.json';
import { scriptedTransport } from './helpers';

interface Stream {
  name: string;
  request_id: string;
  message: string;
  sse: string;
}
interface Recorded {
  name: string;
  status: number;
  body: Record<string, unknown>;
}

const fixture = raw as unknown as {
  backend_sha: string;
  observed_at: string;
  streams: Stream[];
  rejections: Recorded[];
  health: Recorded[];
};

const BASE = 'http://127.0.0.1:8001';
const OBSERVED = Date.parse(fixture.observed_at);
const stream = (name: string) => fixture.streams.find((s) => s.name === name)!;
const recorded = (list: Recorded[], name: string) => list.find((r) => r.name === name)!;
const clone = <T>(v: T): T => JSON.parse(JSON.stringify(v)) as T;
const jsonResponse = (r: Recorded) =>
  new Response(JSON.stringify(r.body), { status: r.status, headers: { 'content-type': 'application/json' } });

function healthCheck(name: string): HealthCheck {
  const r = recorded(fixture.health, name);
  const health = parseHealth(clone(r.body), r.status);
  return health.status === 'ready' ? { kind: 'ready', health } : { kind: 'unavailable', health };
}

async function replay(s: Stream, chunk: number) {
  const stages: string[] = [];
  const outcome = await runStream(scriptedTransport(() => s.sse, chunk), buildRequest(s.message, s.request_id), new AbortController().signal, {
    onProgress: (p) => stages.push(p.stage),
  });
  return { outcome, stages };
}

function finalPayload(s: Stream): unknown {
  const line = s.sse.split('\n').find((l) => l.startsWith('data: ') && l.includes('"type":"final"'))!;
  return (JSON.parse(line.slice(6)) as { payload: unknown }).payload;
}

describe('8001 앱 fixture — SSE', () => {
  it('기준 백엔드와 기록 구성이 기대와 같다', () => {
    expect(fixture.backend_sha).toBe('c0c47be');
    expect(fixture.streams.map((s) => s.name)).toEqual([
      'success_liverpool',
      'success_chelsea',
      'success_munich',
      'prepare_unstructured',
      'prepare_unsupported',
      'normalizer_missing_salary',
      'provider_error',
    ]);
    expect(fixture.rejections.map((r) => r.status)).toEqual([403, 415, 413, 422, 503]);
    expect(fixture.health.map((h) => [h.name, h.status])).toEqual([
      ['ready', 200],
      ['data_expired', 503],
      ['model_unverified', 503],
      ['not_configured', 503],
    ]);
  });

  it.each(fixture.streams.map((s) => [s.name, s] as const))('%s 응답 바이트를 1·7바이트 청크로 그대로 통과한다', async (name, s) => {
    for (const chunk of [1, 7]) {
      const { outcome, stages } = await replay(s, chunk);
      expect(stages).toEqual(['agent', 'render']);
      if (name === 'provider_error') {
        expect(outcome).toMatchObject({ kind: 'server_error', code: 'selection_provider_error' });
      } else {
        expect(outcome.kind).toBe('final');
        if (outcome.kind === 'final') expect(outcome.result).toEqual(parseFinal(finalPayload(s)));
      }
    }
  });

  it('성공 결과는 정확한 문자열 수치와 확인된 모델 출처를 담는다', () => {
    const result = parseFinal(finalPayload(stream('success_chelsea')));
    expect(result.status).toBe('optimal');
    expect(result.conditions).toMatchObject({ price_cap: '5800000000000', salary_cap: '205', allowed_reinforcements: [5, 7] });
    expect(result.squad!.rows).toHaveLength(11);
    expect(typeof result.squad!.total_price).toBe('string');
    expect(result.versions).toMatchObject({ model: 'qwen3:8b', model_digest: 'a'.repeat(64) });
  });

  it('준비 단계에서 막히면 모델을 쓰지 않았다고 기록한다', () => {
    const result = parseFinal(finalPayload(stream('prepare_unstructured')));
    expect(result.issues.map((i) => i.code)).toEqual([
      'prepare.unrecognized_context',
      'prepare.missing_coefficient',
      'prepare.invalid_salary',
    ]);
    expect(result.versions).toMatchObject({ model: 'unused', selection: 'unused' });
  });
});

describe('8001 앱 fixture — 스트림 전 HTTP 거절', () => {
  it.each(fixture.rejections.map((r) => [r.name, r] as const))('%s는 상태 코드를 가진 통신 오류로 끝나고 다시 보내지 않는다', async (_name, r) => {
    const fetchImpl = vi.fn(async () => jsonResponse(r));
    const outcome = await runStream(createLiveTransport(BASE, fetchImpl), buildRequest('요청'), new AbortController().signal);
    expect(outcome).toMatchObject({ kind: 'transport_error', status: r.status });
    expect(fetchImpl).toHaveBeenCalledTimes(1);
  });
});

describe('8001 앱 fixture — GET /health', () => {
  it.each(fixture.health.map((h) => [h.name, h] as const))('%s 본문을 상태 코드와 함께 해석한다', (name, h) => {
    const health = parseHealth(clone(h.body), h.status);
    expect(health.status).toBe(name === 'ready' ? 'ready' : 'unavailable');
    expect(health.reason).toBe(name === 'ready' ? null : name);
    expect(health.busy).toBe(false);
  });

  it('준비 상태에는 모델 확인 기록과 자료 기한이 있다', () => {
    const health = parseHealth(clone(recorded(fixture.health, 'ready').body), 200);
    expect(health.model).toMatchObject({ name: 'qwen3:8b', verification: 'startup_snapshot', ollama_version: '0.33.0' });
    expect(Date.parse(health.material!.valid_until)).toBe(OBSERVED + 24 * 3600 * 1000);
  });

  it('상태 코드와 본문이 어긋나거나 모르는 필드·사유·버전이면 거부한다', () => {
    const ready = () => clone(recorded(fixture.health, 'ready').body);
    expect(() => parseHealth(ready(), 503)).toThrow(ContractError);
    expect(() => parseHealth(clone(recorded(fixture.health, 'data_expired').body), 200)).toThrow(ContractError);
    expect(() => parseHealth({ ...ready(), extra: 1 }, 200)).toThrow(/알 수 없는 필드/);
    expect(() => parseHealth({ ...ready(), schema_version: 'agent-health.v2' }, 200)).toThrow(ContractError);
    expect(() => parseHealth({ ...ready(), status: 'unavailable', reason: 'busy' }, 503)).toThrow(ContractError);
    expect(() => parseHealth({ ...ready(), busy: 'no' }, 200)).toThrow(ContractError);
    const noCheck = ready() as { model: Record<string, unknown> };
    noCheck.model.checked_at = null;
    expect(() => parseHealth(noCheck, 200)).toThrow(/모델 확인/);
  });

  it('상태 확인은 GET 한 번이며 연결 실패·예상 밖 상태를 구분한다', async () => {
    const ok = vi.fn(async (_url: string, _init: RequestInit) => jsonResponse(recorded(fixture.health, 'ready')));
    expect((await createHealthChecker(ok)(BASE)).kind).toBe('ready');
    const [url, init] = ok.mock.calls[0]!;
    expect(url).toBe(`${BASE}/health`);
    expect(init).toMatchObject({ method: 'GET', credentials: 'omit', cache: 'no-store', redirect: 'error' });

    expect((await createHealthChecker(async () => jsonResponse(recorded(fixture.health, 'data_expired')))(BASE)).kind).toBe('unavailable');
    expect(await createHealthChecker(async () => Promise.reject(new TypeError('Failed to fetch')))(BASE)).toEqual({ kind: 'unreachable' });
    expect((await createHealthChecker(async () => new Response('x', { status: 500 }))(BASE)).kind).toBe('invalid');
    expect((await createHealthChecker(async () => new Response('not json', { status: 200 }))(BASE)).kind).toBe('invalid');
  });
});

describe('8001 앱 fixture — 화면', () => {
  const mounted: ReturnType<typeof mount>[] = [];
  afterEach(() => mounted.splice(0).forEach((w) => w.unmount()));

  function mountLive(check: () => Promise<HealthCheck>, s: Stream | null, now = OBSERVED + 3600 * 1000) {
    const checker = vi.fn(async () => check());
    const transport = s ? scriptedTransport((id) => s.sse.replaceAll(`"request_id":"${s.request_id}"`, `"request_id":"${id}"`), 11) : null;
    const w = mount(App, {
      props: { liveBaseUrl: BASE, mockDelayMs: 0, now: () => now, checkHealth: checker, makeTransport: () => transport },
      attachTo: document.body,
    });
    mounted.push(w);
    return { w, checker };
  }

  async function chooseLive(w: ReturnType<typeof mount>) {
    await w.get('input[value=live]').setValue(true);
    await flushPromises();
  }

  it('서버가 준비되면 모델·자료 기한을 보이고 실제 응답을 표로 그린다', async () => {
    const s = stream('success_liverpool');
    const { w, checker } = mountLive(async () => healthCheck('ready'), s);
    await chooseLive(w);
    expect(w.get('[data-testid=server-title]').text()).toBe('서버 준비됨');
    expect(w.get('[data-testid=server-status]').text()).toContain('qwen3:8b');
    expect(w.find('[data-testid=server-past-deadline]').exists()).toBe(false);
    await w.get('textarea').setValue(s.message);
    expect(w.get('button.send').attributes('disabled')).toBeUndefined();
    await w.get('form').trigger('submit');
    await vi.waitFor(() => expect(w.findAll('[data-testid=squad-row]')).toHaveLength(11));
    const total = (finalPayload(s) as { squad: { total_price: string } }).squad.total_price;
    expect(w.get('[data-testid=total-price]').text()).toBe(groupDigits(total));
    // 요청이 끝나면 busy·기한을 다시 읽는다.
    await vi.waitFor(() => expect(checker).toHaveBeenCalledTimes(2));
  });

  it.each([
    ['data_expired', '서버의 합성 자료 기한이 지났습니다'],
    ['model_unverified', '서버가 시작할 때 모델을 확인하지 못했습니다'],
    ['not_configured', '서버 시작 설정이 없습니다'],
  ])('%s 상태면 이유를 보이고 보내기를 막는다', async (name, title) => {
    const { w } = mountLive(async () => healthCheck(name), null);
    await chooseLive(w);
    expect(w.get('[data-testid=server-title]').text()).toBe(title);
    await w.get('textarea').setValue('예산 6조, 급여 210, 리버풀, 4-2-3-1, 5~8강으로 구성해줘');
    expect(w.get('button.send').attributes('disabled')).toBeDefined();
    expect(w.text()).toContain('서버가 준비되지 않아 보낼 수 없습니다');
  });

  it('서버에 연결하지 못하면 주소와 포트를 안내하고, 다시 확인으로 회복한다', async () => {
    let up = false;
    const { w, checker } = mountLive(async () => (up ? healthCheck('ready') : { kind: 'unreachable' }), null);
    await chooseLive(w);
    expect(w.get('[data-testid=server-title]').text()).toBe('서버에 연결하지 못했습니다');
    expect(w.get('[data-testid=server-status]').text()).toContain('5173');
    up = true;
    await w.get('[data-testid=server-status] .refresh').trigger('click');
    await flushPromises();
    expect(checker).toHaveBeenCalledTimes(2);
    expect(w.get('[data-testid=server-title]').text()).toBe('서버 준비됨');
  });

  it('화면 시계로 자료 기한이 지났으면 다시 확인하라고 알린다', async () => {
    const { w } = mountLive(async () => healthCheck('ready'), null, OBSERVED + 2 * 24 * 3600 * 1000);
    await chooseLive(w);
    expect(w.find('[data-testid=server-past-deadline]').exists()).toBe(true);
  });

  it('모의 응답으로 바꾸면 서버 상태 표시를 거둔다', async () => {
    const { w } = mountLive(async () => healthCheck('ready'), null);
    await chooseLive(w);
    expect(w.find('[data-testid=server-status]').exists()).toBe(true);
    await w.get('input[value=mock]').setValue(true);
    expect(w.find('[data-testid=server-status]').exists()).toBe(false);
  });

  it.each([
    ['origin_not_allowed', '이 화면의 주소가 서버 허용 목록에 없습니다'],
    ['json_required', '서버가 JSON이 아닌 요청을 거절했습니다'],
    ['body_too_large', '요청 본문이 서버 한도를 넘었습니다'],
    ['invalid_request', '서버가 요청 형식을 받지 않았습니다'],
    ['agent_unavailable', '추천 서버가 아직 준비되지 않았습니다'],
  ])('%s 거절을 상태 코드별 제목으로 안내한다', async (name, title) => {
    const r = recorded(fixture.rejections, name);
    const fetchImpl = vi.fn(async () => jsonResponse(r));
    const checker = vi.fn(async () => healthCheck('ready'));
    const w = mount(App, {
      props: {
        liveBaseUrl: BASE,
        mockDelayMs: 0,
        now: () => OBSERVED + 3600 * 1000,
        checkHealth: checker,
        makeTransport: () => createLiveTransport(BASE, fetchImpl),
      },
      attachTo: document.body,
    });
    mounted.push(w);
    await chooseLive(w);
    await w.get('textarea').setValue('예산 6조, 급여 210, 리버풀, 4-2-3-1, 5~8강으로 구성해줘');
    await w.get('form').trigger('submit');
    await vi.waitFor(() => expect(w.find('[data-testid=result-title]').exists()).toBe(true));
    expect(w.get('[data-testid=result-title]').text()).toBe(title);
    expect(fetchImpl).toHaveBeenCalledTimes(1);
  });
});
