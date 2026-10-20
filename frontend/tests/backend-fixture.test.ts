/**
 * 백엔드 W4-1 순수 투영/SSE 코드가 만든 합성 fixture를 웹 파서·세션·화면에 그대로 통과시킨다.
 * fixture는 tests/fixtures/generate_backend_fixture.py로 다시 만들 수 있다. 실제 HTTP 서버 연결 검증은 아니다.
 */
import { flushPromises, mount } from '@vue/test-utils';
import { afterEach, describe, expect, it, vi } from 'vitest';
import App from '../src/App.vue';
import { parseFinal, type FinalResult } from '../src/contracts/webAgentV0';
import { buildRequest, runStream } from '../src/services/agentSession';
import { groupDigits } from '../src/utils/format';
import raw from './fixtures/backend-w4-1.json';
import { scriptedTransport } from './helpers';

interface BackendCase {
  name: string;
  message: string;
  kind: 'final' | 'error';
  payload: Record<string, unknown>;
  sse: string;
}

const fixture = raw as unknown as {
  backend_sha: string;
  observed_at: string;
  cases: BackendCase[];
};

const requestIdOf = (c: BackendCase) => `fixture-${c.name.replaceAll('_', '-')}`;
const byName = (name: string) => fixture.cases.find((c) => c.name === name)!;
const OBSERVED = Date.parse(fixture.observed_at);

async function replay(c: BackendCase, chunk: number) {
  const stages: string[] = [];
  const outcome = await runStream(
    scriptedTransport(() => c.sse, chunk),
    buildRequest(c.message, requestIdOf(c)),
    new AbortController().signal,
    { onProgress: (p) => stages.push(p.stage) },
  );
  return { outcome, stages };
}

describe('백엔드 W4-1 fixture', () => {
  it('기준 백엔드와 사례 구성이 기대와 같다', () => {
    expect(fixture.backend_sha).toBe('4ff544d');
    expect(fixture.cases.map((c) => c.name)).toEqual([
      'success_liverpool',
      'success_chelsea',
      'success_munich',
      'prepare_unstructured',
      'prepare_unsupported',
      'normalizer_missing_salary',
      'provider_error',
      'request_deadline',
    ]);
  });

  it.each(fixture.cases.map((c) => [c.name, c] as const))('%s의 SSE 바이트를 1·7바이트 청크로 그대로 통과한다', async (_name, c) => {
    for (const chunk of [1, 7]) {
      const { outcome, stages } = await replay(c, chunk);
      expect(stages[0]).toBe('agent');
      if (c.kind === 'final') {
        expect(outcome.kind).toBe('final');
        if (outcome.kind === 'final') expect(outcome.result).toEqual(parseFinal(c.payload));
      } else {
        expect(outcome).toEqual({ kind: 'server_error', code: c.payload.code, message: c.payload.message });
      }
    }
  });

  it('성공 결과의 금액·ID는 문자열이고 원문 조건과 일치한다', () => {
    const result = parseFinal(byName('success_chelsea').payload);
    expect(result.status).toBe('optimal');
    expect(result.conditions).toMatchObject({
      price_cap: '5800000000000',
      salary_cap: '205',
      team_color: '첼시',
      formation: '4-1-2-3',
      allowed_reinforcements: [5, 7],
    });
    expect(result.squad!.rows).toHaveLength(11);
    for (const row of result.squad!.rows) {
      for (const key of ['spid', 'person_id', 'price', 'salary'] as const) expect(typeof row[key]).toBe('string');
    }
    expect(result.versions.model).toBe('unused');
    expect(result.narration_source).toBe('template');
  });

  it('준비 단계 사유는 prepare.* 코드와 occurrence null로 오고 모델·엔진 버전은 unused다', () => {
    const result: FinalResult = parseFinal(byName('prepare_unstructured').payload);
    expect(result.issues.map((i) => [i.field, i.code])).toEqual([
      ['original', 'prepare.unrecognized_context'],
      ['price_cap', 'prepare.missing_coefficient'],
      ['salary_cap', 'prepare.invalid_salary'],
    ]);
    expect(result.issues.every((i) => i.occurrence === null)).toBe(true);
    expect(result.versions).toMatchObject({ model: 'unused', normalizer: 'unused', engine: 'unused' });
    expect(parseFinal(byName('prepare_unsupported').payload).issues[0]).toMatchObject({ kind: 'unsupported' });
  });
});

describe('백엔드 W4-1 fixture 화면 표시', () => {
  const mounted: ReturnType<typeof mount>[] = [];
  afterEach(() => mounted.splice(0).forEach((w) => w.unmount()));

  function mountWith(c: BackendCase, now: number) {
    const transport = scriptedTransport((id) => c.sse.replaceAll(`"request_id":"${requestIdOf(c)}"`, `"request_id":"${id}"`), 11);
    const w = mount(App, {
      props: { liveBaseUrl: '', mockDelayMs: 0, now: () => now, makeTransport: () => transport },
      attachTo: document.body,
    });
    mounted.push(w);
    return w;
  }

  async function send(w: ReturnType<typeof mount>, message: string) {
    await w.get('input[value=mock]').setValue(true);
    await w.get('textarea').setValue(message);
    await w.get('form').trigger('submit');
    await flushPromises();
  }

  it('기한 안의 성공 결과는 표와 정확한 합계를 보인다', async () => {
    const c = byName('success_liverpool');
    const w = mountWith(c, OBSERVED + 60 * 60 * 1000);
    await send(w, c.message);
    await vi.waitFor(() => expect(w.findAll('[data-testid=squad-row]')).toHaveLength(11));
    const total = (c.payload.squad as { total_price: string }).total_price;
    expect(w.get('[data-testid=total-price]').text()).toBe(groupDigits(total));
  });

  it('받는 시점에 기한이 지난 성공 결과는 표를 보이지 않는다', async () => {
    const c = byName('success_liverpool');
    const w = mountWith(c, OBSERVED + 2 * 24 * 60 * 60 * 1000);
    await send(w, c.message);
    await vi.waitFor(() => expect(w.find('[data-testid=result-title]').exists()).toBe(true));
    expect(w.get('[data-testid=result-title]').text()).toContain('기한');
    expect(w.findAll('[data-testid=squad-row]')).toHaveLength(0);
  });
});
