<script setup lang="ts">
import type { ProgressPayload } from '../contracts/webAgentV1';

/** 서버가 보낸 단계만 순서대로 보여 준다. 진행률을 만들어 내지 않는다. */
defineProps<{
  started: boolean;
  steps: ProgressPayload[];
  running: boolean;
}>();

/** 백엔드가 보내는 단계 이름의 화면 표기. 모르는 단계는 받은 이름을 그대로 보인다. */
const STAGE_LABELS: Record<string, string> = { agent: '조건·구성', render: '결과 확인' };
</script>

<template>
  <section class="progress" aria-label="진행 단계">
    <h2 class="title">진행</h2>
    <p v-if="!started && running" class="waiting">서버 응답을 기다리고 있습니다.</p>
    <ol v-if="steps.length" class="steps" aria-live="polite">
      <li
        v-for="(step, i) in steps"
        :key="i"
        :class="{ current: running && i === steps.length - 1 }"
      >
        <span class="stage" :data-stage="step.stage">{{ STAGE_LABELS[step.stage] ?? step.stage }}</span>
        <span class="message">{{ step.message }}</span>
      </li>
    </ol>
    <p v-else-if="started" class="waiting">요청을 받았습니다.</p>
  </section>
</template>

<style scoped>
.progress {
  border-top: 1px solid var(--rule);
  padding-top: 1rem;
}
.title {
  font-size: var(--step-0);
  font-weight: 700;
  margin-bottom: 0.5rem;
}
.waiting {
  color: var(--ink-2);
  font-size: var(--step--1);
}
.steps {
  list-style: none;
  margin: 0;
  padding: 0;
  display: grid;
  gap: 0.375rem;
}
.steps li {
  display: grid;
  grid-template-columns: 5.5rem 1fr;
  gap: 0.75rem;
  font-size: var(--step--1);
  color: var(--ink-2);
}
.stage {
  color: var(--ink-3);
}
.current .message {
  color: var(--ink);
  font-weight: 500;
}
</style>
