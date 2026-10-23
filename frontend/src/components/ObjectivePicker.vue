<script setup lang="ts">
import { computed } from 'vue';
import type { ObjectiveOption, ObjectiveReason } from '../contracts/agentObjectives';
import type { ObjectiveId } from '../contracts/webAgentV2';
import type { ObjectivesCheck } from '../services/objectives';

/**
 * 요청마다 고르는 추천 방식. 서버 목록(GET /objectives)의 사용 가능 방식만 고를 수 있다.
 * 언어 모델이나 원문이 방식을 정하지 않으며, 사용할 수 없는 방식을 다른 방식으로 바꿔 보내지 않는다.
 */
const props = defineProps<{
  check: ObjectivesCheck | null;
  checking: boolean;
  modelValue: ObjectiveId;
  disabled: boolean;
}>();

const emit = defineEmits<{ 'update:modelValue': [value: ObjectiveId] }>();

const REASON_COPY: Record<ObjectiveReason, string> = {
  not_configured: '서버에 다요소 정책이 설정되지 않았습니다.',
  policy_mismatch: '서버의 정책 파일과 기대 해시가 맞지 않습니다.',
  synthetic_material: '합성 자료에는 양발 정보가 없어 다요소 점수를 계산할 수 없습니다.',
  agent_unavailable: '추천 서버가 준비되지 않았습니다.',
};

const options = computed<ObjectiveOption[]>(() => (props.check?.kind === 'listed' ? props.check.listing.objectives : []));

const status = computed(() => {
  const c = props.check;
  if (props.checking || c === null) return '추천 방식 목록을 확인하고 있습니다.';
  if (c.kind === 'unreachable') return '추천 방식 목록을 받지 못했습니다. 서버를 확인한 뒤 다시 확인을 눌러 주세요.';
  if (c.kind === 'invalid') return `추천 방식 목록을 해석하지 못했습니다 (${c.detail}).`;
  return null;
});

function choose(id: ObjectiveId): void {
  emit('update:modelValue', id);
}
</script>

<template>
  <fieldset class="objective" :disabled="disabled" data-testid="objective-picker">
    <legend>추천 방식</legend>
    <p v-if="status" class="status" data-testid="objective-status">{{ status }}</p>
    <div v-for="option in options" :key="option.id" class="option" :class="{ off: !option.available }" :data-objective="option.id">
      <label>
        <input
          type="radio"
          name="objective"
          :value="option.id"
          :checked="modelValue === option.id"
          :disabled="!option.available"
          @change="choose(option.id)"
        />
        <span class="name">
          <template v-if="option.id === 'ovr'">기존 OVR</template>
          <template v-else>필드 우선·GK 단계·다요소 보조</template>
        </span>
        <span v-if="option.id !== 'ovr'" class="badge" data-testid="objective-experimental">실험</span>
        <span v-if="option.id === 'ovr'" class="quiet">기본</span>
      </label>
      <p class="desc">
        <template v-if="option.id === 'ovr'">예산·급여 상한 안에서 OVR 합이 가장 높은 조합을 찾습니다.</template>
        <template v-else>필드 OVR을 먼저 확보하고 GK OVR을 단계로 고른 뒤, 정해진 허용폭 안에서 실험 다요소 점수를 보조 기준으로 씁니다.</template>
      </p>
      <p v-if="option.policy" class="desc" data-testid="objective-policy">
        정책 {{ option.policy.policy_id }} <span class="quiet" :title="option.policy.version">{{ option.policy.version.slice(0, 12) }}</span>
      </p>
      <p v-if="option.parameters" class="desc quiet">
        GK 단계 필드 양보 최대 {{ option.parameters.gk_exchange }} · 허용폭 필드 {{ option.parameters.outfield_tolerance }}·GK
        {{ option.parameters.gk_tolerance }} · 탐색 {{ option.parameters.search_seconds }}초
      </p>
      <p v-if="!option.available && option.reason" class="reason" data-testid="objective-reason">
        사용할 수 없음: {{ REASON_COPY[option.reason] }}
      </p>
    </div>
    <p v-if="options.length" class="desc quiet">방식은 여기서 고른 값으로만 정합니다. 요청 문장이나 언어 모델이 방식을 바꾸지 않습니다.</p>
  </fieldset>
</template>

<style scoped>
.objective {
  border: 0;
  margin: 0;
  padding: 1rem 0 0;
  border-top: 1px solid var(--rule);
  display: grid;
  gap: 0.75rem;
  font-size: var(--step--1);
  min-width: 0;
}
legend {
  float: left;
  width: 100%;
  font-size: var(--step-0);
  font-weight: 700;
  margin-bottom: 0.25rem;
}
.option {
  display: grid;
  gap: 0.125rem;
}
.option label {
  display: inline-flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 0.375rem;
  cursor: pointer;
  font-weight: 500;
}
.option input {
  accent-color: var(--pitch);
}
.option.off label {
  color: var(--ink-3);
  cursor: not-allowed;
}
.badge {
  border: 1px solid var(--warn);
  color: var(--warn);
  border-radius: 3px;
  padding: 0 0.3rem;
  font-size: 0.75rem;
  font-weight: 700;
}
.desc {
  color: var(--ink-2);
  padding-left: 1.4rem;
  overflow-wrap: anywhere;
}
.quiet {
  color: var(--ink-3);
}
.reason {
  color: var(--warn);
  padding-left: 1.4rem;
}
.status {
  color: var(--ink-2);
}
</style>
