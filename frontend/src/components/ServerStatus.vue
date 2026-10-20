<script setup lang="ts">
import { computed } from 'vue';
import type { HealthReason } from '../contracts/agentHealth';
import type { HealthCheck } from '../services/health';
import { formatTime } from '../utils/format';

const props = defineProps<{
  baseUrl: string;
  check: HealthCheck | null;
  checking: boolean;
  /** 진행 중에는 다시 확인을 막는다. 서버는 한 번에 한 요청만 받는다. */
  locked: boolean;
  now: () => number;
}>();

const emit = defineEmits<{ refresh: [] }>();

const REASON_COPY: Record<HealthReason, { title: string; note: string }> = {
  data_expired: {
    title: '서버의 합성 자료 기한이 지났습니다',
    note: '서버를 새 합성 자료로 다시 시작해야 추천할 수 있습니다. 기한을 늘려 쓰지 않습니다.',
  },
  model_unverified: {
    title: '서버가 시작할 때 모델을 확인하지 못했습니다',
    note: 'Ollama와 지정한 모델 상태를 확인한 뒤 서버를 다시 시작해야 합니다.',
  },
  not_configured: {
    title: '서버 시작 설정이 없습니다',
    note: '서버의 합성 자료·모델 설정이 비어 있어 요청을 받지 않습니다.',
  },
};

const view = computed(() => {
  const c = props.check;
  if (props.checking || c === null) return { tone: 'info', title: '서버 상태를 확인하고 있습니다', note: null };
  switch (c.kind) {
    case 'ready':
      return { tone: 'ok', title: '서버 준비됨', note: null };
    case 'unavailable':
      return { tone: 'bad', ...REASON_COPY[c.health.reason!] };
    case 'unreachable':
      return {
        tone: 'bad',
        title: '서버에 연결하지 못했습니다',
        note: `${props.baseUrl} 서버가 켜져 있는지, 이 화면을 http://localhost:5173 또는 http://127.0.0.1:5173 에서 열었는지 확인해 주세요.`,
      };
    case 'invalid':
      return { tone: 'bad', title: '서버 상태 응답을 해석하지 못했습니다', note: c.detail };
  }
  return { tone: 'info', title: '', note: null };
});

const health = computed(() => (props.check?.kind === 'ready' || props.check?.kind === 'unavailable' ? props.check.health : null));

/** 브라우저 시계 기준 안내일 뿐이다. 실제 만료 판단은 서버가 한다. */
const pastValidUntil = computed(() => {
  const until = health.value?.material?.valid_until;
  return props.check?.kind === 'ready' && until !== undefined && props.now() >= Date.parse(until);
});
</script>

<template>
  <section class="server" :data-tone="view.tone" aria-live="polite" data-testid="server-status">
    <div class="head">
      <h2 class="title" data-testid="server-title">{{ view.title }}</h2>
      <button type="button" class="refresh" :disabled="checking || locked" @click="emit('refresh')">다시 확인</button>
    </div>
    <p v-if="view.note" class="note">{{ view.note }}</p>
    <dl v-if="health" class="facts">
      <div v-if="health.model">
        <dt>모델</dt>
        <dd>
          {{ health.model.name }} <span class="quiet" :title="health.model.digest">{{ health.model.digest.slice(0, 12) }}</span>
        </dd>
      </div>
      <div v-if="health.model?.checked_at">
        <dt>모델 확인</dt>
        <dd>{{ formatTime(health.model.checked_at) }} (서버 시작 때)</dd>
      </div>
      <div v-if="health.material">
        <dt>자료 기준</dt>
        <dd>{{ formatTime(health.material.observed_at) }}</dd>
      </div>
      <div v-if="health.material">
        <dt>유효 기한</dt>
        <dd data-testid="server-valid-until">{{ formatTime(health.material.valid_until) }}</dd>
      </div>
    </dl>
    <p v-if="pastValidUntil" class="warn" data-testid="server-past-deadline">
      이 화면의 시계로는 유효 기한이 지났습니다. 다시 확인해 주세요.
    </p>
    <p v-if="check?.kind === 'ready' && check.health.busy" class="warn" data-testid="server-busy">
      확인한 시점에 서버가 다른 요청을 처리 중이었습니다. 보내면 거절될 수 있습니다.
    </p>
  </section>
</template>

<style scoped>
.server {
  --tone: var(--info);
  border-top: 1px solid var(--rule);
  padding-top: 1rem;
  display: grid;
  gap: 0.375rem;
  font-size: var(--step--1);
}
.server[data-tone='ok'] {
  --tone: var(--ok);
}
.server[data-tone='bad'] {
  --tone: var(--bad);
}
.head {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 1rem;
}
.title {
  font-size: var(--step-0);
  font-weight: 700;
  color: var(--tone);
}
.refresh {
  border: 1px solid var(--rule);
  border-radius: 4px;
  background: var(--surface);
  padding: 0.25rem 0.625rem;
  font-size: var(--step--1);
  color: var(--ink-2);
  cursor: pointer;
  flex-shrink: 0;
}
.refresh:disabled {
  cursor: not-allowed;
  opacity: 0.6;
}
.note {
  color: var(--ink-2);
  overflow-wrap: anywhere;
}
.facts {
  margin: 0.25rem 0 0;
  display: grid;
  gap: 0.125rem;
}
.facts div {
  display: grid;
  grid-template-columns: 4.75rem 1fr;
  gap: 0.5rem;
}
.facts dt {
  color: var(--ink-3);
}
.facts dd {
  margin: 0;
  overflow-wrap: anywhere;
}
.quiet {
  color: var(--ink-3);
  font-variant-numeric: tabular-nums;
}
.warn {
  color: var(--warn);
}
</style>
