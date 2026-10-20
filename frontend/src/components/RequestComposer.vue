<script setup lang="ts">
import { computed, ref } from 'vue';
import { MAX_MESSAGE_CODE_POINTS } from '../contracts/webAgentV0';
import { codePointLength, isBlank } from '../utils/format';

const props = defineProps<{
  running: boolean;
  /** 전송을 막는 사유(연결 방식 미선택 등). null이면 막지 않는다. */
  blockedReason: string | null;
}>();

const emit = defineEmits<{ submit: [message: string] }>();

const message = ref('');

/** 예시는 입력란만 채운다. 사용자가 확인하고 직접 보낸다. */
const examples = [
  '리버풀 4-2-3-1, 예산 6조 BP, 급여 210 이하, 5~8강 카드로 스쿼드 짜줘',
  '첼시 4-1-2-3으로 예산 5조 8000억 BP, 급여 상한 205, 5강,7강만 써서 추천해줘',
  '바이에른 뮌헨 4-2-2-2, 예산 3조 BP, 6~11강으로 맞춰줘',
];

const length = computed(() => codePointLength(message.value));
const tooLong = computed(() => length.value > MAX_MESSAGE_CODE_POINTS);
const blank = computed(() => isBlank(message.value));
const disabled = computed(() => props.running || blank.value || tooLong.value || props.blockedReason !== null);

const hint = computed(() => {
  if (props.running) return '진행 중인 요청이 끝나면 다시 보낼 수 있습니다.';
  if (props.blockedReason) return props.blockedReason;
  if (tooLong.value) return `${MAX_MESSAGE_CODE_POINTS.toLocaleString('ko-KR')}자를 넘었습니다. 줄여서 보내 주세요. 자동으로 자르지 않습니다.`;
  if (blank.value) return '요청을 입력하면 보낼 수 있습니다.';
  return null;
});

function submit(): void {
  if (disabled.value) return;
  emit('submit', message.value);
}

function onKeydown(event: KeyboardEvent): void {
  if (event.key === 'Enter' && (event.ctrlKey || event.metaKey)) {
    event.preventDefault();
    submit();
  }
}
</script>

<template>
  <form class="composer" @submit.prevent="submit">
    <label class="label" for="request-message">요청</label>
    <p id="request-help" class="help">
      예산(BP), 급여 상한, 클럽, 포메이션, 허용 강화 다섯 가지를 한 문장에 모두 적어 주세요. 빠진 조건은 추측하지 않고 되묻습니다.
    </p>
    <textarea
      id="request-message"
      v-model="message"
      class="input"
      rows="5"
      aria-describedby="request-help request-count request-hint"
      :aria-invalid="tooLong"
      @keydown="onKeydown"
    />
    <div class="meta">
      <span id="request-count" class="count" :class="{ over: tooLong }">
        {{ length.toLocaleString('ko-KR') }} / {{ MAX_MESSAGE_CODE_POINTS.toLocaleString('ko-KR') }}자
      </span>
      <button type="submit" class="send" :disabled="disabled">
        {{ running ? '추천 중' : '추천 받기' }}
      </button>
    </div>
    <p v-if="hint" id="request-hint" class="hint" role="status">{{ hint }}</p>

    <div class="examples">
      <p class="examples-title">예시로 채우기</p>
      <ul>
        <li v-for="text in examples" :key="text">
          <button type="button" class="example" :disabled="running" @click="message = text">{{ text }}</button>
        </li>
      </ul>
    </div>
  </form>
</template>

<style scoped>
.composer {
  display: grid;
  gap: 0.5rem;
}
.label {
  font-size: var(--step-1);
  font-weight: 700;
}
.help {
  color: var(--ink-2);
  font-size: var(--step--1);
  max-width: 40ch;
}
.input {
  width: 100%;
  resize: vertical;
  min-height: 8.5rem;
  padding: 0.75rem 0.875rem;
  border: 1px solid var(--rule);
  border-radius: 4px;
  background: var(--surface);
  line-height: 1.55;
}
.input:focus-visible {
  border-color: var(--focus);
}
.meta {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 1rem;
}
.count {
  color: var(--ink-3);
  font-size: var(--step--1);
}
.count.over {
  color: var(--bad);
  font-weight: 500;
}
.send {
  border: 0;
  border-radius: 4px;
  padding: 0.625rem 1.25rem;
  background: var(--pitch);
  color: var(--chalk);
  font-weight: 700;
  cursor: pointer;
}
.send:disabled {
  background: var(--rule);
  color: var(--ink-3);
  cursor: not-allowed;
}
.hint {
  color: var(--ink-2);
  font-size: var(--step--1);
}
.examples {
  margin-top: 1rem;
}
.examples-title {
  font-size: var(--step--1);
  color: var(--ink-3);
  margin-bottom: 0.25rem;
}
.examples ul {
  list-style: none;
  margin: 0;
  padding: 0;
}
.example {
  display: block;
  width: 100%;
  text-align: left;
  background: none;
  border: 0;
  border-top: 1px solid var(--rule);
  padding: 0.5rem 0;
  font-size: var(--step--1);
  color: var(--ink-2);
  cursor: pointer;
}
.example:hover:not(:disabled) {
  color: var(--ink);
}
.example:disabled {
  cursor: not-allowed;
  opacity: 0.6;
}
</style>
