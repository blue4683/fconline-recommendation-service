import { computed, onBeforeUnmount, ref, shallowRef } from 'vue';
import { MAX_MESSAGE_CODE_POINTS, type ProgressPayload } from '../contracts/webAgentV1';
import { codePointLength, isBlank } from '../utils/format';
import { buildRequest, runStream, type Outcome } from './agentSession';
import type { Transport } from './transport';

export type Phase = 'idle' | 'running' | 'done';

export interface RecommendationOptions {
  now?: () => number;
  /** 만료 감지 주기(ms). */
  expiryPollMs?: number;
}

/**
 * 단일 화면의 요청 상태.
 * - 진행 중에는 새 전송을 막는다(중복 실행 방지·자동 재전송 없음).
 * - 새 전송 즉시 이전 결과를 지운다. 이전 요청의 늦은 이벤트는 세대 번호로 무시한다.
 * - 성공 결과의 valid_until이 지나면 만료로 바꾸고 되돌리지 않는다. 브라우저 시계로 유효성을
 *   확정하거나 기한을 연장하지 않는다.
 */
export function useRecommendation(options: RecommendationOptions = {}) {
  const now = options.now ?? (() => Date.now());
  const phase = ref<Phase>('idle');
  const started = ref(false);
  const progress = ref<ProgressPayload[]>([]);
  const outcome = shallowRef<Outcome | null>(null);
  const expired = ref(false);
  const lastRequestId = ref<string | null>(null);

  let generation = 0;
  let controller: AbortController | null = null;
  let timer: ReturnType<typeof setInterval> | null = null;

  const validUntil = computed(() => {
    const o = outcome.value;
    if (o?.kind !== 'final' || !o.result.squad) return null;
    return o.result.snapshot?.valid_until ?? null;
  });

  function checkExpiry(): void {
    const until = validUntil.value;
    if (until === null || expired.value) return;
    if (now() >= Date.parse(until)) {
      expired.value = true;
      stopTimer();
    }
  }

  function stopTimer(): void {
    if (timer !== null) clearInterval(timer);
    timer = null;
  }

  function onVisibility(): void {
    if (typeof document !== 'undefined' && document.visibilityState === 'visible') checkExpiry();
  }
  if (typeof document !== 'undefined') document.addEventListener('visibilitychange', onVisibility);

  function canSend(message: string): boolean {
    return phase.value !== 'running' && !isBlank(message) && codePointLength(message) <= MAX_MESSAGE_CODE_POINTS;
  }

  async function send(message: string, transport: Transport): Promise<void> {
    if (!canSend(message)) return;
    const mine = ++generation;
    controller?.abort();
    controller = new AbortController();
    stopTimer();
    phase.value = 'running';
    started.value = false;
    progress.value = [];
    outcome.value = null;
    expired.value = false;

    const request = buildRequest(message);
    lastRequestId.value = request.request_id;
    const result = await runStream(transport, request, controller.signal, {
      onStarted: () => {
        if (mine === generation) started.value = true;
      },
      onProgress: (p) => {
        if (mine === generation) progress.value = [...progress.value, p];
      },
    });
    if (mine !== generation) return;
    outcome.value = result;
    phase.value = 'done';
    controller = null;
    if (validUntil.value !== null) {
      checkExpiry();
      if (!expired.value) timer = setInterval(checkExpiry, options.expiryPollMs ?? 1000);
    }
  }

  function reset(): void {
    generation += 1;
    controller?.abort();
    controller = null;
    stopTimer();
    phase.value = 'idle';
    started.value = false;
    progress.value = [];
    outcome.value = null;
    expired.value = false;
    lastRequestId.value = null;
  }

  function dispose(): void {
    reset();
    if (typeof document !== 'undefined') document.removeEventListener('visibilitychange', onVisibility);
  }

  onBeforeUnmount(dispose);

  return { phase, started, progress, outcome, expired, lastRequestId, canSend, send, checkExpiry, reset, dispose };
}
