/**
 * 추천 방식 선택 화면과 v2 결과 표시. 모의 transport와 v2 서버 기록(합성)만 쓴다.
 */
import { flushPromises, mount } from '@vue/test-utils';
import { afterEach, describe, expect, it, vi } from 'vitest';
import App from '../src/App.vue';
import ResultView from '../src/components/ResultView.vue';
import { parseHealth } from '../src/contracts/agentHealth';
import { parseObjectives } from '../src/contracts/agentObjectives';
import { parseFinalV2 } from '../src/contracts/webAgentV2';
import { finalForV2, MOCK_POLICY, type ScenarioId } from '../src/mocks/fixtures';
import { createMockTransport } from '../src/mocks/mockTransport';
import type { AnyStreamRequest, Transport } from '../src/services/transport';
import type { HealthCheck } from '../src/services/health';
import { createLiveTransport } from '../src/services/liveTransport';
import type { ObjectivesCheck } from '../src/services/objectives';
import { groupDigits } from '../src/utils/format';
import rawV1 from './fixtures/backend-agent-app.json';
import raw from './fixtures/backend-agent-v2.json';
import { FIXED_NOW, scriptedTransport } from './helpers';

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
const fixture = raw as unknown as { observed_at: string; streams: Stream[]; rejections: Recorded[]; objectives: Recorded[] };
const v1 = rawV1 as unknown as { health: Recorded[] };
const clone = <T>(v: T): T => JSON.parse(JSON.stringify(v)) as T;
const stream = (name: string) => fixture.streams.find((s) => s.name === name)!;
const recorded = (list: Recorded[], name: string) => list.find((r) => r.name === name)!;
const BASE = 'http://127.0.0.1:8001';
const OBSERVED = Date.parse(fixture.observed_at);
const MESSAGE = '예산 6조, 리버풀, 4-2-3-1, 5~8강으로 구성해줘';

function finalPayload(s: Stream): Record<string, unknown> {
  const line = s.sse.split('\n').find((l) => l.startsWith('data: ') && l.includes('"type":"final"'))!;
  return (JSON.parse(line.slice(6)) as { payload: Record<string, unknown> }).payload;
}

function objectivesCheck(name: string): ObjectivesCheck {
  const r = recorded(fixture.objectives, name);
  return { kind: 'listed', listing: parseObjectives(clone(r.body), r.status) };
}

function observedReady(): HealthCheck {
  const r = recorded(v1.health, 'observed_ready');
  return { kind: 'ready', health: parseHealth(clone(r.body), r.status) };
}

const mounted: ReturnType<typeof mount>[] = [];
afterEach(() => mounted.splice(0).forEach((w) => w.unmount()));

function mountApp(props: Record<string, unknown> = {}) {
  const w = mount(App, { props: { liveBaseUrl: '', mockDelayMs: 0, now: () => FIXED_NOW, ...props }, attachTo: document.body });
  mounted.push(w);
  return w;
}

/** 모의 transport를 감싸 보낸 요청을 기록한다. */
function recordingMock() {
  const sent: AnyStreamRequest[] = [];
  const make = (_m: unknown, scenario: () => ScenarioId) => {
    const inner = createMockTransport({ scenario, now: () => FIXED_NOW });
    const t: Transport = { mode: 'mock', open: (r, s) => (sent.push(r), inner.open(r, s)) };
    return t;
  };
  return { sent, make };
}

async function submit(w: ReturnType<typeof mount>, text = MESSAGE) {
  await w.get('textarea').setValue(text);
  await w.get('form').trigger('submit');
}

describe('추천 방식 선택 — 모의 모드', () => {
  it('두 방식을 보이고 기본은 OVR이며 다요소는 실험·정책 ID로 표시한다', async () => {
    const w = mountApp();
    expect(w.find('[data-testid=objective-picker]').exists()).toBe(false);
    await w.get('input[value=mock]').setValue(true);
    const picker = w.get('[data-testid=objective-picker]');
    const radios = picker.findAll('input[name=objective]');
    expect(radios.map((r) => r.attributes('value'))).toEqual(['ovr', 'gk_priority_multi_factor']);
    expect((radios[0]!.element as HTMLInputElement).checked).toBe(true);
    expect(radios.every((r) => r.attributes('disabled') === undefined)).toBe(true);
    expect(picker.get('[data-testid=objective-experimental]').text()).toBe('실험');
    expect(picker.get('[data-testid=objective-policy]').text()).toContain(MOCK_POLICY.policy_id);
    expect(picker.text()).toContain('언어 모델이 방식을 바꾸지 않습니다');
  });

  it('OVR로 보내면 v2 요청에 ovr를 싣고 점수 열·단계 없이 필드/GK OVR을 보인다', async () => {
    const { sent, make } = recordingMock();
    const w = mountApp({ makeTransport: make });
    await w.get('input[value=mock]').setValue(true);
    await submit(w);
    await vi.waitFor(() => expect(w.findAll('[data-testid=squad-row]')).toHaveLength(11));
    expect(sent).toHaveLength(1);
    expect(sent[0]).toMatchObject({ contract_version: 'web-agent.v2', objective: 'ovr', message: MESSAGE });
    const expected = finalForV2('optimal', FIXED_NOW, 'ovr')!.squad!;
    expect(w.get('[data-testid=summary-ovr-split]').text()).toBe(`${expected.outfield_ovr} · ${expected.gk_ovr}`);
    expect(w.find('[data-testid=row-score]').exists()).toBe(false);
    expect(w.find('[data-testid=summary-score]').exists()).toBe(false);
    expect(w.find('[data-testid=stages]').exists()).toBe(false);
    expect(w.get('[data-testid=result-objective]').text()).toContain('기존 OVR');
    expect(w.get('[data-testid=result-title]').text()).toContain('최적');
  });

  it('다요소를 고르면 서버 점수 문자열·합계·세 단계·정책·실험 표시를 그대로 보인다', async () => {
    const { sent, make } = recordingMock();
    const w = mountApp({ makeTransport: make });
    await w.get('input[value=mock]').setValue(true);
    await w.get('input[value=gk_priority_multi_factor]').setValue(true);
    await submit(w);
    await vi.waitFor(() => expect(w.findAll('[data-testid=squad-row]')).toHaveLength(11));
    expect(sent[0]).toMatchObject({ contract_version: 'web-agent.v2', objective: 'gk_priority_multi_factor' });
    const squad = finalForV2('optimal', FIXED_NOW, 'gk_priority_multi_factor')!.squad!;
    // 서버 문자열 그대로: 끝자리 0도 지우지 않는다("42.00").
    expect(w.findAll('[data-testid=row-score]').map((c) => c.text())).toEqual(squad.rows.map((r) => r.score));
    expect(squad.rows[0]!.score).toBe('42.00');
    expect(w.get('[data-testid=total-score]').text()).toBe(squad.total_score);
    expect(w.get('[data-testid=summary-score]').text()).toBe(squad.total_score);
    const stages = w.findAll('[data-testid=stages] li');
    expect(stages.map((li) => li.attributes('data-stage'))).toEqual(['outfield_ovr', 'gk_ovr', 'multi_factor']);
    expect(stages.map((li) => li.find('.stage-name').text())).toEqual(['필드 OVR 최대', 'GK OVR', '다요소 합']);
    expect(stages.map((li) => li.find('.stage-status').text())).toEqual(['최적 확인', '최적 확인', '최적 확인']);
    expect(stages[2]!.find('.stage-value').text()).toBe(`${squad.total_score}점`);
    const objective = w.get('[data-testid=result-objective]');
    expect(objective.text()).toContain('필드 우선·GK 단계·다요소 보조');
    expect(objective.text()).toContain('실험');
    expect(w.get('[data-testid=result-policy]').text()).toContain(MOCK_POLICY.policy_id);
    // 다요소 결과에 OVR 방식 설명(OVR 합 최대)을 붙이지 않는다.
    expect(w.get('[data-testid=result]').text()).not.toContain('OVR 합이 가장 높은 조합');
  });

  it('다요소 가능해는 미확정·실행 안 함 단계를 구분해 보인다', async () => {
    const w = mountApp();
    await w.get('input[value=mock]').setValue(true);
    await w.get('#scenario').setValue('feasible');
    await w.get('input[value=gk_priority_multi_factor]').setValue(true);
    await submit(w);
    await vi.waitFor(() => expect(w.find('[data-testid=stages]').exists()).toBe(true));
    const stages = w.findAll('[data-testid=stages] li');
    expect(stages.map((li) => li.find('.stage-status').text())).toEqual(['최적 확인', '시간 내 미확정', '실행 안 함']);
    expect(stages[2]!.find('.stage-value').text()).toBe('—');
    expect(w.get('[data-testid=result-title]').text()).toContain('최적인지는 확인하지 못했습니다');
  });

  it.each([
    ['not_configured', '다요소 정책이 설정되지 않았습니다'],
    ['synthetic_material', '양발 정보가 없어'],
    ['policy_mismatch', '기대 해시가 맞지 않습니다'],
  ])('목록이 %s이면 다요소를 끄고 사유를 보이며 OVR은 그대로 보낸다', async (listing, reason) => {
    const w = mountApp();
    await w.get('input[value=mock]').setValue(true);
    await w.get('#mock-listing').setValue(listing);
    const multi = w.get('input[value=gk_priority_multi_factor]');
    expect(multi.attributes('disabled')).toBeDefined();
    expect(w.get('[data-testid=objective-reason]').text()).toContain(reason);
    expect(w.find('[data-testid=objective-policy]').exists()).toBe(false);
    await submit(w);
    await vi.waitFor(() => expect(w.findAll('[data-testid=squad-row]')).toHaveLength(11));
  });

  it('고른 다요소가 사용 불가가 되면 OVR로 바꾸지 않고 보내기를 막는다', async () => {
    const { sent, make } = recordingMock();
    const w = mountApp({ makeTransport: make });
    await w.get('input[value=mock]').setValue(true);
    await w.get('input[value=gk_priority_multi_factor]').setValue(true);
    await w.get('#mock-listing').setValue('not_configured');
    await w.get('textarea').setValue(MESSAGE);
    expect(w.get('button.send').attributes('disabled')).toBeDefined();
    expect(w.text()).toContain('고른 추천 방식을 지금 사용할 수 없습니다');
    expect((w.get('input[value=gk_priority_multi_factor]').element as HTMLInputElement).checked).toBe(true);
    expect((w.get('input[value=ovr]').element as HTMLInputElement).checked).toBe(false);
    await w.get('form').trigger('submit');
    expect(sent).toHaveLength(0);
    // 사용자가 직접 OVR을 고르면 보낼 수 있다.
    await w.get('input[value=ovr]').setValue(true);
    expect(w.get('button.send').attributes('disabled')).toBeUndefined();
  });

  it('409 objective_unavailable은 분명한 실패로 보이고 다시 보내지 않는다', async () => {
    let opens = 0;
    const makeTransport = (_m: unknown, scenario: () => ScenarioId): Transport => {
      const inner = createMockTransport({ scenario, now: () => FIXED_NOW });
      return { mode: 'mock', open: (r, s) => (opens++, inner.open(r, s)) };
    };
    const w = mountApp({ makeTransport });
    await w.get('input[value=mock]').setValue(true);
    await w.get('#scenario').setValue('objective_unavailable');
    await w.get('input[value=gk_priority_multi_factor]').setValue(true);
    await submit(w);
    await vi.waitFor(() => expect(w.find('[data-testid=result-title]').exists()).toBe(true));
    expect(w.get('[data-testid=result-title]').text()).toBe('고른 추천 방식을 지금 서버에서 사용할 수 없습니다');
    expect(w.text()).toContain('OVR 방식으로 바꾸거나 자동으로 다시 보내지 않습니다');
    expect(w.get('.detail').text()).toBe('HTTP 409 objective_unavailable');
    await flushPromises();
    expect(opens).toBe(1);
    expect(w.findAll('[data-testid=squad-row]')).toHaveLength(0);
  });
});

describe('추천 방식 선택 — 실제 연결(기록 재생)', () => {
  function mountLive(objectives: () => Promise<ObjectivesCheck>, transport: Transport | null) {
    const health = vi.fn(async () => observedReady());
    const listing = vi.fn(objectives);
    const w = mountApp({
      liveBaseUrl: BASE,
      now: () => OBSERVED + 3600 * 1000,
      checkHealth: health,
      checkObjectives: listing,
      makeTransport: () => transport,
    });
    return { w, health, listing };
  }

  const replay = (s: Stream) => scriptedTransport((id) => s.sse.replaceAll(`"request_id":"${s.request_id}"`, `"request_id":"${id}"`), 13);

  it('서버 목록으로 다요소를 고르고 관측 기록의 점수·단계·정책을 그대로 보인다', async () => {
    const s = stream('observed_multi_success');
    const { w, listing, health } = mountLive(async () => objectivesCheck('observed_available'), replay(s));
    await w.get('input[value=live]').setValue(true);
    await flushPromises();
    expect(listing).toHaveBeenCalledTimes(1);
    expect(listing).toHaveBeenCalledWith(BASE);
    await w.get('input[value=gk_priority_multi_factor]').setValue(true);
    await submit(w, s.message);
    await vi.waitFor(() => expect(w.findAll('[data-testid=squad-row]')).toHaveLength(11));
    const result = parseFinalV2(finalPayload(s));
    expect(w.findAll('[data-testid=row-score]').map((c) => c.text())).toEqual(result.squad!.rows.map((r) => r.score));
    expect(w.get('[data-testid=total-score]').text()).toBe(result.squad!.total_score);
    expect(w.get('[data-testid=total-price]').text()).toBe(groupDigits(result.squad!.total_price));
    expect(w.get('[data-testid=summary-ovr-split]').text()).toBe(`${result.squad!.outfield_ovr} · ${result.squad!.gk_ovr}`);
    expect(w.get('[data-testid=result-policy]').text()).toContain(result.objective.policy!.policy_id);
    expect(w.findAll('[data-testid=stages] li .stage-value').map((c) => c.text())).toEqual([
      String(result.objective.stages[0]!.value),
      String(result.objective.stages[1]!.value),
      `${result.squad!.total_score}점`,
    ]);
    expect(w.get('[data-testid=answer]').text()).toContain('실험 다요소 점수');
    // 요청이 끝나면 서버 상태와 방식 목록을 다시 읽는다.
    await vi.waitFor(() => expect(listing).toHaveBeenCalledTimes(2));
    expect(health).toHaveBeenCalledTimes(2);
  });

  it.each([
    [{ kind: 'unreachable' } as ObjectivesCheck, '추천 방식 목록을 받지 못했습니다'],
    [{ kind: 'invalid', detail: '예상하지 않은 HTTP 404' } as ObjectivesCheck, '해석하지 못했습니다'],
  ])('방식 목록을 확인하지 못하면 방식을 추측하지 않고 보내기를 막는다', async (check, text) => {
    const { w } = mountLive(async () => check, null);
    await w.get('input[value=live]').setValue(true);
    await flushPromises();
    expect(w.get('[data-testid=objective-status]').text()).toContain(text);
    expect(w.find('input[name=objective]').exists()).toBe(false);
    await w.get('textarea').setValue(MESSAGE);
    expect(w.get('button.send').attributes('disabled')).toBeDefined();
    expect(w.text()).toContain('추천 방식 목록을 확인하지 못해 보낼 수 없습니다');
  });

  it('실제 409 objective_unavailable 본문을 받으면 한 번만 보내고 OVR로 바꾸지 않는다', async () => {
    const r = recorded(fixture.rejections, 'objective_unavailable');
    const fetchImpl = vi.fn(async (_u: string, _i: RequestInit) =>
      new Response(JSON.stringify(r.body), { status: r.status, headers: { 'content-type': 'application/json' } }),
    );
    const { w } = mountLive(async () => objectivesCheck('observed_available'), createLiveTransport(BASE, fetchImpl));
    await w.get('input[value=live]').setValue(true);
    await flushPromises();
    await w.get('input[value=gk_priority_multi_factor]').setValue(true);
    await submit(w);
    await vi.waitFor(() => expect(w.find('[data-testid=result-title]').exists()).toBe(true));
    expect(w.get('[data-testid=result-title]').text()).toBe('고른 추천 방식을 지금 서버에서 사용할 수 없습니다');
    await flushPromises();
    expect(fetchImpl).toHaveBeenCalledTimes(1);
    expect(JSON.parse(fetchImpl.mock.calls[0]![1].body as string).objective).toBe('gk_priority_multi_factor');
  });

  it('다른 409(서버 사용 중)는 기존 안내를 유지한다', async () => {
    const fetchImpl = vi.fn(async () => new Response('{"detail":{"code":"agent_busy"}}', { status: 409 }));
    const { w } = mountLive(async () => objectivesCheck('observed_available'), createLiveTransport(BASE, fetchImpl));
    await w.get('input[value=live]').setValue(true);
    await flushPromises();
    await submit(w);
    await vi.waitFor(() => expect(w.find('[data-testid=result-title]').exists()).toBe(true));
    expect(w.get('[data-testid=result-title]').text()).toBe('다른 추천 요청이 진행 중입니다');
  });
});

describe('ResultView — v2 표시', () => {
  it('HTML처럼 생긴 정책 ID를 실행하지 않고 텍스트로 보인다', () => {
    const payload = clone(finalForV2('optimal', FIXED_NOW, 'gk_priority_multi_factor')!);
    payload.objective.policy!.policy_id = '<img src=x onerror=alert(1)>';
    const result = parseFinalV2(payload);
    const w = mount(ResultView, { props: { outcome: { kind: 'final', result }, expired: false } });
    mounted.push(w);
    expect(w.find('img').exists()).toBe(false);
    expect(w.get('[data-testid=result-policy]').text()).toContain('<img src=x onerror=alert(1)>');
  });

  it('만료된 다요소 결과는 표·점수·단계를 거둔다', () => {
    const result = parseFinalV2(clone(finalForV2('optimal', FIXED_NOW, 'gk_priority_multi_factor')));
    const w = mount(ResultView, { props: { outcome: { kind: 'final', result }, expired: true } });
    mounted.push(w);
    expect(w.get('[data-testid=result-title]').text()).toContain('기한이 지났습니다');
    expect(w.find('[data-testid=stages]').exists()).toBe(false);
    expect(w.find('[data-testid=row-score]').exists()).toBe(false);
  });
});
