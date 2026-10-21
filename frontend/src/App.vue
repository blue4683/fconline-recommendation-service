<script setup lang="ts">
import { computed, ref, shallowRef, watch } from 'vue';
import ProgressSteps from './components/ProgressSteps.vue';
import RequestComposer from './components/RequestComposer.vue';
import ResultView from './components/ResultView.vue';
import ServerStatus from './components/ServerStatus.vue';
import { SCENARIOS, type ScenarioId } from './mocks/fixtures';
import { createMockTransport } from './mocks/mockTransport';
import { createHealthChecker, type HealthCheck, type HealthChecker } from './services/health';
import { createLiveTransport, normalizeBaseUrl } from './services/liveTransport';
import type { Transport, TransportMode } from './services/transport';
import { useRecommendation } from './services/useRecommendation';

const props = withDefaults(
  defineProps<{
    /** 실제 연결 기준 주소. 기본은 VITE_FRS_API_BASE_URL. 비어 있으면 실제 연결을 선택할 수 없다. */
    liveBaseUrl?: string;
    /** 테스트용 교체 지점. */
    makeTransport?: (mode: TransportMode, scenario: () => ScenarioId) => Transport | null;
    /** 실제 연결의 GET /health 확인. 테스트용 교체 지점. */
    checkHealth?: HealthChecker;
    now?: () => number;
    mockDelayMs?: number;
  }>(),
  {
    liveBaseUrl: () => import.meta.env.VITE_FRS_API_BASE_URL ?? '',
    makeTransport: undefined,
    checkHealth: undefined,
    now: undefined,
    mockDelayMs: 450,
  },
);

const baseUrl = computed(() => normalizeBaseUrl(props.liveBaseUrl));
const mode = ref<TransportMode | null>(null);
const scenario = ref<ScenarioId>('optimal');

const rec = useRecommendation({ now: props.now });
const nowFn = () => (props.now ?? Date.now)();

// 실제 연결의 서버 상태. 연결 방식을 고를 때·다시 확인을 누를 때·요청이 끝날 때만 한 번씩 조회한다(주기 조회 없음).
const checker = props.checkHealth ?? createHealthChecker();
const health = shallowRef<HealthCheck | null>(null);
const checking = ref(false);
let healthGeneration = 0;

async function refreshHealth(): Promise<void> {
  const base = baseUrl.value;
  if (!base) return;
  const generation = ++healthGeneration;
  checking.value = true;
  let result: HealthCheck;
  try {
    result = await checker(base);
  } catch {
    result = { kind: 'unreachable' };
  }
  if (generation !== healthGeneration) return;
  health.value = result;
  checking.value = false;
}

watch(mode, (m) => {
  if (m === 'live') {
    void refreshHealth();
  } else {
    healthGeneration += 1;
    health.value = null;
    checking.value = false;
  }
});

function transportFor(m: TransportMode): Transport | null {
  if (props.makeTransport) return props.makeTransport(m, () => scenario.value);
  if (m === 'mock') return createMockTransport({ scenario: () => scenario.value, now: props.now, delayMs: props.mockDelayMs });
  return baseUrl.value ? createLiveTransport(baseUrl.value) : null;
}

const blockedReason = computed(() => {
  if (mode.value === null) return '연결 방식을 먼저 고르세요.';
  if (mode.value === 'live') {
    if (!baseUrl.value) return '실제 연결 주소가 아직 정해지지 않았습니다.';
    const h = health.value;
    if (checking.value || h === null) return '서버 상태를 확인한 뒤 보낼 수 있습니다.';
    if (h.kind === 'unavailable') return '서버가 준비되지 않아 보낼 수 없습니다. 서버를 확인한 뒤 다시 확인을 눌러 주세요.';
    if (h.kind === 'unreachable') return '서버에 연결하지 못해 보낼 수 없습니다.';
    if (h.kind === 'invalid') return '서버 상태를 확인하지 못해 보낼 수 없습니다.';
  }
  return null;
});

const running = computed(() => rec.phase.value === 'running');

// 실제 요청이 끝나면 서버의 busy·기한 표시를 새로 읽는다.
watch(running, (now, before) => {
  if (before && !now && mode.value === 'live') void refreshHealth();
});

function onSubmit(message: string): void {
  if (mode.value === null) return;
  const transport = transportFor(mode.value);
  if (!transport) return;
  void rec.send(message, transport);
}
</script>

<template>
  <div class="shell">
    <header class="masthead">
      <div class="brand">
        <h1>FRS 스쿼드 추천</h1>
        <p class="data-note" data-testid="data-note">
          합성 가상 리그 자료로만 동작합니다. 선수 이름과 수치는 실제와 무관합니다.
        </p>
      </div>
      <fieldset class="mode" :disabled="running">
        <legend>연결 방식</legend>
        <label>
          <input v-model="mode" type="radio" name="mode" value="mock" />
          모의 응답
        </label>
        <label :class="{ unavailable: !baseUrl }">
          <input v-model="mode" type="radio" name="mode" value="live" :disabled="!baseUrl" />
          실제 연결
        </label>
      </fieldset>
    </header>

    <p v-if="mode" class="mode-banner" :data-mode="mode" data-testid="mode-banner">
      <template v-if="mode === 'mock'">
        모의 응답 모드입니다. 서버에 요청을 보내지 않고, 원문을 해석하지 않은 채 아래에서 고른 결과를 재생합니다.
      </template>
      <template v-else>실제 연결 모드입니다. {{ baseUrl }} 에 요청을 보냅니다.</template>
    </p>
    <p v-else class="mode-banner" data-testid="mode-banner">
      연결 방식을 고르면 시작할 수 있습니다.
      <template v-if="!baseUrl">실제 연결은 서버 주소가 정해진 뒤에 열립니다.</template>
    </p>

    <main class="workspace">
      <aside class="side">
        <RequestComposer :running="running" :blocked-reason="blockedReason" @submit="onSubmit" />
        <div v-if="mode === 'mock'" class="scenario">
          <label for="scenario">모의 결과</label>
          <select id="scenario" v-model="scenario" :disabled="running">
            <option v-for="s in SCENARIOS" :key="s.id" :value="s.id">{{ s.label }}</option>
          </select>
        </div>
        <ServerStatus
          v-if="mode === 'live' && baseUrl"
          :base-url="baseUrl"
          :check="health"
          :checking="checking"
          :locked="running"
          :now="nowFn"
          @refresh="refreshHealth"
        />
        <ProgressSteps
          v-if="rec.phase.value !== 'idle'"
          :started="rec.started.value"
          :steps="rec.progress.value"
          :running="running"
        />
      </aside>

      <section class="stage" aria-label="결과">
        <ResultView v-if="rec.outcome.value" :outcome="rec.outcome.value" :expired="rec.expired.value" />
        <div v-else-if="running" class="placeholder" data-testid="pending">
          <p>결과는 서버 검증이 끝난 뒤 한 번에 표시됩니다.</p>
        </div>
        <div v-else class="placeholder">
          <p class="lead">조건 다섯 가지를 적어 보내면 이곳에 검증된 11명이 경기장 배치도와 표로 나타납니다.</p>
          <p>예산과 급여 상한을 넘지 않는 조합 중 OVR 합이 높은 스쿼드를 찾습니다.</p>
        </div>
      </section>
    </main>
  </div>
</template>

<style scoped>
.shell {
  max-width: 78rem;
  margin: 0 auto;
  padding: 1.5rem clamp(1rem, 3vw, 2.5rem) 3rem;
}
.masthead {
  display: flex;
  flex-wrap: wrap;
  align-items: flex-end;
  justify-content: space-between;
  gap: 1rem 2rem;
  padding-bottom: 1rem;
  border-bottom: 2px solid var(--ink);
}
h1 {
  font-size: var(--step-3);
  line-height: 1.1;
  font-weight: 700;
  letter-spacing: -0.02em;
}
.data-note {
  margin-top: 0.375rem;
  font-size: var(--step--1);
  color: var(--ink-2);
}
.mode {
  border: 0;
  margin: 0;
  padding: 0;
  display: flex;
  align-items: center;
  gap: 1rem;
  font-size: var(--step--1);
}
.mode legend {
  float: left;
  color: var(--ink-3);
  margin-right: 0.25rem;
}
.mode label {
  display: inline-flex;
  align-items: center;
  gap: 0.375rem;
  cursor: pointer;
}
.mode input {
  accent-color: var(--pitch);
}
.mode .unavailable {
  color: var(--ink-3);
  cursor: not-allowed;
}
.mode-banner {
  padding: 0.625rem 0;
  font-size: var(--step--1);
  color: var(--ink-2);
  border-bottom: 1px solid var(--rule);
}
.mode-banner[data-mode='mock'] {
  color: var(--warn);
}
.mode-banner[data-mode='live'] {
  color: var(--info);
}
.workspace {
  display: grid;
  grid-template-columns: minmax(18rem, 24rem) 1fr;
  gap: 3rem;
  padding-top: 2rem;
  align-items: start;
}
.side {
  display: grid;
  gap: 1.5rem;
}
.scenario {
  display: grid;
  gap: 0.25rem;
  font-size: var(--step--1);
  color: var(--ink-2);
}
.scenario select {
  padding: 0.4rem 0.5rem;
  border: 1px solid var(--rule);
  border-radius: 4px;
  background: var(--surface);
}
.stage {
  min-width: 0;
}
.placeholder {
  color: var(--ink-2);
  max-width: 46ch;
  display: grid;
  gap: 0.5rem;
}
.placeholder .lead {
  font-size: var(--step-1);
  color: var(--ink);
}
@media (max-width: 52rem) {
  .workspace {
    grid-template-columns: 1fr;
    gap: 2rem;
  }
}
</style>
