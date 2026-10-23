import { flushPromises, mount } from '@vue/test-utils';
import { afterEach, describe, expect, it, vi } from 'vitest';
import App from '../src/App.vue';
import ResultView from '../src/components/ResultView.vue';
import { REQUEST_EXAMPLES } from '../src/contracts/requestFormat';
import { finalFor, finalForV2, optimalWithNames, withObjective, type ScenarioId } from '../src/mocks/fixtures';
import { createMockTransport } from '../src/mocks/mockTransport';
import { parseHealth } from '../src/contracts/agentHealth';
import type { HealthCheck } from '../src/services/health';
import { createLiveTransport } from '../src/services/liveTransport';
import fixture from './fixtures/backend-agent-app.json';
import type { Transport } from '../src/services/transport';
import { controlledTransport, envelopeV2 as envelope, FIXED_NOW, listedObjectives, sse } from './helpers';

const READY_HEALTH: HealthCheck = {
  kind: 'ready',
  health: parseHealth(JSON.parse(JSON.stringify(fixture.health[0]!.body)), fixture.health[0]!.status),
};

const mounted: ReturnType<typeof mount>[] = [];
afterEach(() => {
  mounted.splice(0).forEach((w) => w.unmount());
  vi.useRealTimers();
});

function mountApp(props: Record<string, unknown> = {}) {
  const w = mount(App, { props: { liveBaseUrl: '', mockDelayMs: 0, now: () => FIXED_NOW, ...props }, attachTo: document.body });
  mounted.push(w);
  return w;
}

async function chooseMock(w: ReturnType<typeof mount>, scenario?: ScenarioId) {
  await w.get('input[value=mock]').setValue(true);
  if (scenario) await w.get('#scenario').setValue(scenario);
}

async function submit(w: ReturnType<typeof mount>, text = REQUEST_EXAMPLES[0]!) {
  await w.get('textarea').setValue(text);
  await w.get('form').trigger('submit');
}

describe('App', () => {
  it('자료 모드를 추측하지 않고, 연결 방식을 고르기 전에는 보내지 않는다', async () => {
    const w = mountApp();
    expect(w.get('[data-testid=data-note]').text()).toContain('서버 확인 결과');
    await w.get('textarea').setValue('리버풀');
    expect(w.get('button.send').attributes('disabled')).toBeDefined();
    expect(w.text()).toContain('연결 방식을 먼저 고르세요');
    // 서버 주소가 없으면 실제 연결을 고를 수 없다.
    expect(w.get('input[value=live]').attributes('disabled')).toBeDefined();
  });

  it('모의 모드를 화면에 표시하고 정상 결과의 표와 합계를 그대로 보인다', async () => {
    const w = mountApp();
    await chooseMock(w, 'optimal');
    expect(w.get('[data-testid=mode-banner]').text()).toContain('모의 응답 모드');
    await submit(w);
    await flushPromises();
    await vi.waitFor(() => expect(w.findAll('[data-testid=squad-row]')).toHaveLength(11));
    const expected = finalFor('optimal', FIXED_NOW)!.squad!;
    expect(w.get('[data-testid=total-price]').text()).toBe('5,630,700,000,000');
    expect(expected.total_price).toBe('5630700000000');
    expect(w.get('[data-testid=result-title]').text()).toContain('최적');
  });

  it('34자리 BP와 2^53 초과 급여를 손실 없이 표시한다', async () => {
    // 과거 wire의 큰 정수 운반 검사는 결과 컴포넌트에 직접 주입한다. 새 요청 예시에 노출하지 않는다.
    const w = mount(ResultView, { props: { outcome: { kind: 'final', result: finalFor('huge_numbers', FIXED_NOW)! }, expired: false } });
    mounted.push(w);
    const prices = w.findAll('[data-testid=row-price]').map((c) => c.text());
    expect(prices[0]).toBe('900,000,000,000,000,000,000,000,000,000,001');
    expect(w.findAll('[data-testid=row-salary]')[1]!.text()).toBe('9,007,199,254,740,993');
  });

  it.each([
    ['needs_clarification', '조건을 보완'],
    ['infeasible', '스쿼드가 없습니다'],
    ['unknown', '결론을 내지 못했습니다'],
    ['data_expired', '자료 기한'],
    ['internal_error', '내부 오류'],
    ['selection_error', '조건 추출 모델을 사용할 수 없어'],
    ['request_deadline', '응답 기한 안에'],
    ['protocol_error', '형식과 달라'],
    ['disconnect', '통신하지 못했습니다'],
  ] as [ScenarioId, string][])('%s 상태는 표 없이 구분된 안내를 보인다', async (scenario, title) => {
    const w = mountApp();
    await chooseMock(w, scenario);
    await submit(w);
    await vi.waitFor(() => expect(w.find('[data-testid=result-title]').exists()).toBe(true));
    expect(w.get('[data-testid=result-title]').text()).toContain(title);
    expect(w.findAll('[data-testid=squad-row]')).toHaveLength(0);
    if (scenario === 'needs_clarification') expect(w.get('[data-testid=issues]').text()).toContain('예산');
  });

  it('final 전에는 진행 단계만 보이고 표·설명을 공개하지 않는다', async () => {
    const { transport, opened } = controlledTransport();
    const w = mountApp({ makeTransport: () => transport });
    await chooseMock(w);
    await submit(w);
    await flushPromises();
    const s = opened[0]!;
    s.push(sse('started', envelope(s.requestId, 0, 'started', { data_mode: 'synthetic' })));
    s.push(sse('progress', envelope(s.requestId, 1, 'progress', { stage: 'build', message: '탐색 중' })));
    await flushPromises();
    expect(w.text()).toContain('탐색 중');
    expect(w.find('[data-testid=pending]').exists()).toBe(true);
    expect(w.findAll('[data-testid=squad-row]')).toHaveLength(0);
    expect(w.find('[data-testid=answer]').exists()).toBe(false);
    // 진행 중에는 중복 전송을 막는다.
    expect(w.get('button.send').attributes('disabled')).toBeDefined();
    await w.get('form').trigger('submit');
    expect(opened).toHaveLength(1);

    s.push(sse('final', envelope(s.requestId, 2, 'final', finalForV2('optimal', FIXED_NOW, 'ovr'))));
    s.close();
    await vi.waitFor(() => expect(w.findAll('[data-testid=squad-row]')).toHaveLength(11));
  });

  it('새 요청이 실패하면 이전 성공 표가 남지 않고, 자동 재전송하지 않는다', async () => {
    let scenario: ScenarioId = 'optimal';
    let opens = 0;
    const makeTransport = (): Transport => {
      const inner = createMockTransport({ scenario: () => scenario, now: () => FIXED_NOW });
      return { mode: 'mock', open: (r, s) => (opens++, inner.open(r, s)) };
    };
    const w = mountApp({ makeTransport });
    await chooseMock(w);
    await submit(w);
    await vi.waitFor(() => expect(w.findAll('[data-testid=squad-row]')).toHaveLength(11));
    scenario = 'disconnect';
    await submit(w, '다른 요청');
    expect(w.findAll('[data-testid=squad-row]')).toHaveLength(0);
    await vi.waitFor(() => expect(w.get('[data-testid=result-title]').text()).toContain('통신하지 못했습니다'));
    await flushPromises();
    expect(opens).toBe(2);
    expect(w.findAll('[data-testid=squad-row]')).toHaveLength(0);
  });

  it('HTML처럼 생긴 이름과 설명을 실행하지 않고 텍스트로 보인다', async () => {
    const payload = withObjective(
      optimalWithNames(['<img src=x onerror=alert(1)>', '<b>굵게</b>', ...Array.from({ length: 9 }, (_, i) => `가상${i}`)], FIXED_NOW),
      'ovr',
    );
    const { transport, opened } = controlledTransport();
    const w = mountApp({ makeTransport: () => transport });
    await chooseMock(w);
    await submit(w);
    await flushPromises();
    const s = opened[0]!;
    s.push(sse('started', envelope(s.requestId, 0, 'started', { data_mode: 'synthetic' })));
    s.push(sse('final', envelope(s.requestId, 1, 'final', payload)));
    s.close();
    await vi.waitFor(() => expect(w.findAll('[data-testid=squad-row]')).toHaveLength(11));
    expect(w.find('img').exists()).toBe(false);
    expect(w.find('.name b').exists()).toBe(false);
    expect(w.get('[data-testid=answer]').text()).toBe('<img src=x onerror=alert(1)>');
  });

  it('표시한 결과도 기한이 지나면 만료로 바꾸고 표를 거둔다', async () => {
    let clock = FIXED_NOW;
    const w = mountApp({ now: () => clock });
    await chooseMock(w, 'expiring_soon');
    await submit(w);
    await vi.waitFor(() => expect(w.findAll('[data-testid=squad-row]')).toHaveLength(11));
    clock = FIXED_NOW + 31_000;
    document.dispatchEvent(new Event('visibilitychange'));
    await flushPromises();
    expect(w.get('[data-testid=result-title]').text()).toContain('기한이 지났습니다');
    expect(w.findAll('[data-testid=squad-row]')).toHaveLength(0);
    // 시계를 되돌려도 만료를 되돌리지 않는다.
    clock = FIXED_NOW;
    document.dispatchEvent(new Event('visibilitychange'));
    await flushPromises();
    expect(w.findAll('[data-testid=squad-row]')).toHaveLength(0);
  });

  it('받는 순간 이미 기한이 지난 성공 결과는 표시하지 않는다', async () => {
    const w = mountApp({
      now: () => FIXED_NOW + 2 * 24 * 60 * 60 * 1000,
      makeTransport: (_m: unknown, sc: () => ScenarioId) =>
        createMockTransport({ scenario: sc, now: () => FIXED_NOW }),
    });
    await chooseMock(w, 'optimal');
    await submit(w);
    await vi.waitFor(() => expect(w.get('[data-testid=result-title]').text()).toContain('기한이 지났습니다'));
    expect(w.findAll('[data-testid=squad-row]')).toHaveLength(0);
  });

  it('공백뿐인 원문과 4,000 코드포인트 초과 원문은 보내지 않고 자르지 않는다', async () => {
    const { transport, opened } = controlledTransport();
    const w = mountApp({ makeTransport: () => transport });
    await chooseMock(w);
    await submit(w, '   \n ');
    expect(opened).toHaveLength(0);

    const over = '😀'.repeat(4001);
    await submit(w, over);
    expect(opened).toHaveLength(0);
    expect((w.get('textarea').element as HTMLTextAreaElement).value).toBe(over);
    expect(w.text()).toContain('4,001 / 4,000자');

    await submit(w, '😀'.repeat(4000));
    await flushPromises();
    expect(opened).toHaveLength(1);
  });

  it('Ctrl+Enter로 보내고, 예시 선택은 입력란만 채운다', async () => {
    const { transport, opened } = controlledTransport();
    const w = mountApp({ makeTransport: () => transport });
    await chooseMock(w);
    await w.findAll('.example')[0]!.trigger('click');
    expect(opened).toHaveLength(0);
    expect((w.get('textarea').element as HTMLTextAreaElement).value).toContain('리버풀');
    await w.get('textarea').trigger('keydown', { key: 'Enter', ctrlKey: true });
    await flushPromises();
    expect(opened).toHaveLength(1);
  });

  it('입력 안내와 예시는 쉼표로 나눈 정형 조건문 형식이다', async () => {
    const w = mountApp();
    const help = w.get('#request-help').text();
    expect(help).toContain('쉼표로 나눠');
    expect(help).toContain('310');
    expect(help).toContain('입력을 생략');
    for (const label of ['예산', '급여 상한', '클럽', '포메이션', '강화']) expect(help).toContain(label);
    const shown = w.findAll('.example').map((b) => b.text());
    expect(shown).toEqual([...REQUEST_EXAMPLES]);
    for (const text of shown) {
      // 백엔드 후보 준비가 되묻는 형태(BP 접미사, "이하", 클럽·포메이션 한 절)를 예시에 쓰지 않는다.
      expect(text).not.toMatch(/BP|이하|짜줘/);
      expect(text).toMatch(/^예산 [^,]+, [^,]+, [0-9-]+, .+(으로|로) 구성해줘$/);
      expect(text).not.toContain('급여');
    }
  });

  it('정형 조건문이 아닌 요청에는 준비 단계 사유를 항목별로 보인다', async () => {
    const w = mountApp();
    await chooseMock(w, 'input_format');
    await submit(w, '리버풀 4-2-3-1, 예산 6조 BP, 급여 210 이하, 5~8강으로 구성해줘');
    await vi.waitFor(() => expect(w.find('[data-testid=issues]').exists()).toBe(true));
    expect(w.get('[data-testid=result-title]').text()).toContain('조건을 보완');
    const items = w.findAll('[data-testid=issues] li').map((li) => li.find('.issue-field').text());
    expect(items).toEqual(['원문', '예산', '급여 상한']);
    expect(w.get('[data-testid=issues]').text()).toContain('정형 조건문으로 다시 입력');
    expect(w.find('[data-testid=squad-row]').exists()).toBe(false);
  });

  it('진행 표시는 서버가 실제로 보낸 agent·render 단계만 보인다', async () => {
    const w = mountApp();
    await chooseMock(w, 'optimal');
    await submit(w);
    await vi.waitFor(() => expect(w.findAll('[data-testid=squad-row]')).toHaveLength(11));
    const stages = w.findAll('.steps .stage').map((s) => s.attributes('data-stage'));
    expect(stages).toEqual(['agent', 'render']);
    expect(w.findAll('.steps .stage').map((s) => s.text())).toEqual(['조건·구성', '결과 확인']);
  });

  it('error 이벤트는 공개 코드별 제목과 서버 안내를 보인다', async () => {
    const w = mountApp();
    await chooseMock(w, 'selection_error');
    await submit(w);
    await vi.waitFor(() => expect(w.find('[data-testid=result-title]').exists()).toBe(true));
    expect(w.get('[data-testid=result-title]').text()).toBe('조건 추출 모델을 사용할 수 없어 요청을 처리하지 못했습니다');
    expect(w.text()).toContain('조건 추출 모델을 사용할 수 없습니다. 잠시 후 새 요청을 보내 주세요.');
    expect(w.get('.detail').text()).toBe('오류 코드 selection_provider_error');
  });

  it.each([
    [409, '다른 추천 요청이 진행 중입니다'],
    [413, '요청 본문이 서버 한도를 넘었습니다'],
    [422, '서버가 요청 형식을 받지 않았습니다'],
    [503, '추천 서버가 아직 준비되지 않았습니다'],
    [500, '서버와 통신하지 못했습니다'],
  ])('스트림 전 HTTP %i 거절을 구분해 안내하고 다시 보내지 않는다', async (status, title) => {
    const fetchImpl = vi.fn(async () => new Response('{"code":"x"}', { status, headers: { 'content-type': 'application/json' } }));
    const w = mountApp({
      liveBaseUrl: 'http://127.0.0.1:8001',
      checkHealth: async () => READY_HEALTH,
      checkObjectives: async () => listedObjectives(),
      makeTransport: (m: string) => (m === 'live' ? createLiveTransport('http://127.0.0.1:8001', fetchImpl) : null),
    });
    await w.get('input[value=live]').setValue(true);
    await flushPromises();
    await submit(w);
    await vi.waitFor(() => expect(w.find('[data-testid=result-title]').exists()).toBe(true));
    expect(w.get('[data-testid=result-title]').text()).toBe(title);
    expect(fetchImpl).toHaveBeenCalledTimes(1);
  });

  it('이전 요청의 늦은 이벤트는 현재 화면을 바꾸지 못한다', async () => {
    const { transport, opened } = controlledTransport();
    const w = mountApp({ makeTransport: () => transport });
    await chooseMock(w);
    await submit(w);
    await flushPromises();
    const first = opened[0]!;
    first.push(sse('started', envelope(first.requestId, 0, 'started', { data_mode: 'synthetic' })));
    first.push(sse('error', envelope(first.requestId, 1, 'error', { code: 'x', message: '첫 요청 실패' })));
    first.close();
    await vi.waitFor(() => expect(w.get('[data-testid=result-title]').text()).toContain('오류'));

    await submit(w, '두 번째 요청');
    await flushPromises();
    const second = opened[1]!;
    second.push(sse('started', envelope(second.requestId, 0, 'started', { data_mode: 'synthetic' })));
    // 첫 요청 ID로 된 final이 두 번째 스트림에 섞여 오면 성공으로 보이지 않는다.
    second.push(sse('final', envelope(first.requestId, 1, 'final', finalForV2('optimal', FIXED_NOW, 'ovr'))));
    second.close();
    await vi.waitFor(() => expect(w.get('[data-testid=result-title]').text()).toContain('형식과 달라'));
    expect(w.findAll('[data-testid=squad-row]')).toHaveLength(0);
  });
});
