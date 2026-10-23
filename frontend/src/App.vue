<script setup lang="ts">
import { computed, ref, shallowRef, watch } from 'vue';
import ObjectivePicker from './components/ObjectivePicker.vue';
import ProgressSteps from './components/ProgressSteps.vue';
import RequestComposer from './components/RequestComposer.vue';
import ResultView from './components/ResultView.vue';
import ServerStatus from './components/ServerStatus.vue';
import { optionFor, parseObjectives } from './contracts/agentObjectives';
import type { DataMode } from './contracts/webAgentV1';
import { DEFAULT_OBJECTIVE, type ObjectiveId } from './contracts/webAgentV2';
import { OBJECTIVE_LISTINGS, objectivesResponse, SCENARIOS, type ObjectiveListingId, type ScenarioId } from './mocks/fixtures';
import { createMockTransport } from './mocks/mockTransport';
import { createHealthChecker, type HealthCheck, type HealthChecker } from './services/health';
import { createObjectivesChecker, type ObjectivesCheck, type ObjectivesChecker } from './services/objectives';
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
    /** 실제 연결의 GET /objectives 확인. 테스트용 교체 지점. */
    checkObjectives?: ObjectivesChecker;
    now?: () => number;
    mockDelayMs?: number;
  }>(),
  {
    liveBaseUrl: () => import.meta.env.VITE_FRS_API_BASE_URL ?? '',
    makeTransport: undefined,
    checkHealth: undefined,
    checkObjectives: undefined,
    now: undefined,
    mockDelayMs: 450,
  },
);

const baseUrl = computed(() => normalizeBaseUrl(props.liveBaseUrl));
const mode = ref<TransportMode | null>(null);
const scenario = ref<ScenarioId>('optimal');
/** 요청마다 사용자가 고르는 추천 방식. 기본은 OVR이며 서버 목록에서 사용 가능한 방식만 고를 수 있다. */
const objective = ref<ObjectiveId>(DEFAULT_OBJECTIVE);
const mockListing = ref<ObjectiveListingId>('available');

const rec = useRecommendation({ now: props.now });
const nowFn = () => (props.now ?? Date.now)();

// 실제 연결의 서버 상태. 연결 방식을 고를 때·다시 확인을 누를 때·요청이 끝날 때만 한 번씩 조회한다(주기 조회 없음).
const checker = props.checkHealth ?? createHealthChecker();
const health = shallowRef<HealthCheck | null>(null);
const checking = ref(false);
let healthGeneration = 0;

// 실제 연결의 방식 목록. 서버 상태와 같은 시점에만 한 번씩 조회한다.
const objectivesChecker = props.checkObjectives ?? createObjectivesChecker();
const liveObjectives = shallowRef<ObjectivesCheck | null>(null);
const checkingObjectives = ref(false);

async function refreshHealth(): Promise<void> {
  const base = baseUrl.value;
  if (!base) return;
  const generation = ++healthGeneration;
  checking.value = true;
  checkingObjectives.value = true;
  const [healthResult, objectivesResult] = await Promise.all([
    checker(base).catch((): HealthCheck => ({ kind: 'unreachable' })),
    objectivesChecker(base).catch((): ObjectivesCheck => ({ kind: 'unreachable' })),
  ]);
  if (generation !== healthGeneration) return;
  health.value = healthResult;
  liveObjectives.value = objectivesResult;
  checking.value = false;
  checkingObjectives.value = false;
}

/** 모의 방식 목록. 실제 파서를 그대로 통과시킨다. */
const mockObjectives = computed<ObjectivesCheck>(() => {
  const raw = objectivesResponse(mockListing.value);
  return { kind: 'listed', listing: parseObjectives(raw.body, raw.status) };
});

const objectives = computed<ObjectivesCheck | null>(() => {
  if (mode.value === 'mock') return mockObjectives.value;
  if (mode.value === 'live') return liveObjectives.value;
  return null;
});

const listing = computed(() => (objectives.value?.kind === 'listed' ? objectives.value.listing : null));

watch(mode, (m) => {
  rec.reset();
  if (m === 'live') {
    void refreshHealth();
  } else {
    healthGeneration += 1;
    health.value = null;
    liveObjectives.value = null;
    checking.value = false;
    checkingObjectives.value = false;
  }
});

const dataMode = computed<DataMode | null>(() => {
  if (mode.value === 'mock') return 'synthetic';
  if (mode.value !== 'live') return null;
  const result = rec.outcome.value;
  if (result?.kind === 'final') return result.result.data_mode;
  return health.value?.kind === 'ready' || health.value?.kind === 'unavailable' ? health.value.health.data_mode : null;
});

function transportFor(m: TransportMode): Transport | null {
  if (props.makeTransport) return props.makeTransport(m, () => scenario.value);
  if (m === 'mock') {
    return createMockTransport({ scenario: () => scenario.value, now: props.now, delayMs: props.mockDelayMs, listing: () => listing.value });
  }
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
  if (checkingObjectives.value || objectives.value === null) return '추천 방식 목록을 확인한 뒤 보낼 수 있습니다.';
  if (objectives.value.kind !== 'listed') return '추천 방식 목록을 확인하지 못해 보낼 수 없습니다.';
  if (optionFor(listing.value, objective.value)?.available !== true) {
    return '고른 추천 방식을 지금 사용할 수 없습니다. 사용 가능한 방식을 골라 주세요.';
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
  void rec.send(message, transport, objective.value);
}
</script>

<template>
  <div class="shell">
    <header class="masthead">
      <div class="brand">
        <h1>FRS 스쿼드 추천</h1>
        <p class="data-note" data-testid="data-note">
          <template v-if="dataMode === 'observed'">넥슨 데이터센터 관측 자료를 사용합니다. 가격은 참고가격이며 거래를 보장하지 않습니다.</template>
          <template v-else-if="dataMode === 'synthetic'">합성 가상 리그 자료입니다. 선수 이름과 수치는 실제와 무관합니다.</template>
          <template v-else>자료 모드는 연결 방식을 고른 뒤 서버 확인 결과에 따라 표시합니다.</template>
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
        <ObjectivePicker
          v-if="mode"
          v-model="objective"
          :check="objectives"
          :checking="mode === 'live' && checkingObjectives"
          :disabled="running"
        />
        <div v-if="mode === 'mock'" class="scenario">
          <label for="scenario">모의 결과</label>
          <select id="scenario" v-model="scenario" :disabled="running">
            <option v-for="s in SCENARIOS" :key="s.id" :value="s.id">{{ s.label }}</option>
          </select>
          <label for="mock-listing">모의 방식 목록</label>
          <select id="mock-listing" v-model="mockListing" :disabled="running">
            <option v-for="l in OBJECTIVE_LISTINGS" :key="l.id" :value="l.id">{{ l.label }}</option>
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
          <p class="lead">예산·클럽·포메이션·강화 네 가지 조건을 적어 보내면 이곳에 검증된 11명이 경기장 배치도와 표로 나타납니다.</p>
          <p>급여 상한은 310으로 고정되며 입력을 생략할 수 있습니다. 기본 방식은 예산과 급여 상한을 넘지 않는 조합 중 OVR 합이 높은 스쿼드를 찾습니다. 추천 방식은 왼쪽에서 요청마다 고릅니다.</p>
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
