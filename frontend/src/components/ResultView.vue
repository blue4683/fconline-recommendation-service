<script setup lang="ts">
import { computed } from 'vue';
import type { Conditions, FinalResult, FinalStatus } from '../contracts/webAgentV0';
import type { Outcome } from '../services/agentSession';
import { formatTime, groupDigits, koreanUnits } from '../utils/format';
import FormationPitch from './FormationPitch.vue';
import SquadTable from './SquadTable.vue';

const props = defineProps<{ outcome: Outcome; expired: boolean }>();

type Tone = 'ok' | 'info' | 'warn' | 'bad';

const FIELD_LABELS: Record<string, string> = {
  price_cap: '예산',
  salary_cap: '급여 상한',
  team_color: '클럽',
  formation: '포메이션',
  allowed_reinforcements: '허용 강화',
  unsupported: '미지원 조건',
  original: '원문',
  extraction: '추출 결과',
};

const STATUS_COPY: Record<FinalStatus, { title: string; note: string | null; tone: Tone }> = {
  optimal: {
    title: '조건을 지키는 최적 스쿼드입니다',
    note: '주어진 합성 자료와 조건에서 OVR 합이 가장 높은 조합입니다. 실제 경기 성능을 보장하지 않습니다.',
    tone: 'ok',
  },
  feasible: {
    title: '조건을 지키는 스쿼드입니다. 최적인지는 확인하지 못했습니다',
    note: '제한 시간 안에 더 나은 조합이 없는지 증명하지 못했습니다.',
    tone: 'info',
  },
  needs_clarification: {
    title: '조건을 보완해 다시 요청해 주세요',
    note: '아래 사유를 고친 완전한 요청을 새로 보내면 됩니다. 이전 요청과 합치지 않습니다.',
    tone: 'warn',
  },
  infeasible: {
    title: '보유한 자료에서는 조건을 만족하는 스쿼드가 없습니다',
    note: '현재 합성 자료 기준의 결과이며, 조건 자체가 불가능하다는 뜻은 아닙니다.',
    tone: 'warn',
  },
  unknown: {
    title: '제한 시간 안에 결론을 내지 못했습니다',
    note: '해가 없다는 뜻이 아닙니다. 조건을 바꾸거나 다시 요청할 수 있습니다.',
    tone: 'warn',
  },
  data_expired: {
    title: '자료 기한이 지나 결과를 보여 줄 수 없습니다',
    note: '자료가 갱신된 뒤 다시 요청해 주세요.',
    tone: 'bad',
  },
  internal_error: {
    title: '서버 내부 오류로 결과를 만들지 못했습니다',
    note: '다시 시도하려면 요청을 새로 보내 주세요.',
    tone: 'bad',
  },
  llm_unavailable: {
    title: '언어 모델을 사용할 수 없어 요청을 처리하지 못했습니다',
    note: '다시 시도하려면 요청을 새로 보내 주세요.',
    tone: 'bad',
  },
};

const final = computed<FinalResult | null>(() => (props.outcome.kind === 'final' ? props.outcome.result : null));
const showSquad = computed(() => final.value?.squad != null && !props.expired);

const header = computed<{ title: string; note: string | null; tone: Tone; detail?: string }>(() => {
  const o = props.outcome;
  switch (o.kind) {
    case 'final':
      if (props.expired) {
        return {
          title: '이 추천은 기한이 지났습니다',
          note: '만료된 결과는 현재 추천으로 쓸 수 없습니다. 필요하면 요청을 새로 보내 주세요.',
          tone: 'bad',
          detail: o.result.snapshot?.valid_until ? `유효 기한 ${formatTime(o.result.snapshot.valid_until)}` : undefined,
        };
      }
      return o.result.code
        ? { ...STATUS_COPY[o.result.status], detail: `사유 코드 ${o.result.code}` }
        : STATUS_COPY[o.result.status];
    case 'server_error':
      return { title: '추천 중 오류가 발생했습니다', note: o.message, tone: 'bad', detail: `오류 코드 ${o.code}` };
    case 'protocol_error':
      return {
        title: '서버 응답이 약속한 형식과 달라 결과를 표시하지 않습니다',
        note: '다시 시도하려면 요청을 새로 보내 주세요.',
        tone: 'bad',
        detail: o.detail,
      };
    case 'transport_error':
      return { title: '서버와 통신하지 못했습니다', note: o.detail, tone: 'bad' };
    case 'aborted':
      return { title: '요청 읽기를 멈췄습니다', note: '서버 계산이 함께 취소되었는지는 알 수 없습니다.', tone: 'warn' };
  }
  return { title: '', note: null, tone: 'info' };
});

/** 해 없음·미확정일 때만 자리별 후보 수를 보여 준다. 성공 결과에는 표가 이미 있다. */
const coverage = computed(() => {
  const f = final.value;
  if (!f || !f.coverage || (f.status !== 'infeasible' && f.status !== 'unknown')) return null;
  return f.coverage;
});
const emptySlots = computed(() => coverage.value?.filter((c) => c.count === 0).map((c) => c.slot) ?? []);

function conditionLines(c: Conditions) {
  return [
    { label: '예산', value: `${koreanUnits(c.price_cap)} BP`, exact: `${groupDigits(c.price_cap)} BP` },
    { label: '급여 상한', value: groupDigits(c.salary_cap), exact: null },
    { label: '클럽', value: c.team_color, exact: null },
    { label: '포메이션', value: c.formation, exact: null },
    { label: '허용 강화', value: c.allowed_reinforcements.map((l) => `${l}강`).join(', '), exact: null },
  ];
}

function versionText(v: Record<string, string>): string {
  return Object.entries(v)
    .map(([k, value]) => `${k} ${value}`)
    .join(', ');
}
</script>

<template>
  <section class="result" :data-tone="header.tone" aria-live="polite" data-testid="result">
    <h2 class="headline" data-testid="result-title">{{ header.title }}</h2>
    <p v-if="header.note" class="note">{{ header.note }}</p>
    <p v-if="header.detail" class="detail">{{ header.detail }}</p>

    <template v-if="final && !expired">
      <ul v-if="final.issues.length" class="issues" data-testid="issues">
        <li v-for="(issue, i) in final.issues" :key="i">
          <span class="issue-field">{{ FIELD_LABELS[issue.field] ?? issue.field }}</span>
          <span class="issue-body">
            <span v-if="issue.span" class="issue-span" data-testid="issue-span">“{{ issue.span.text }}”</span>
            <span>{{ issue.message }}</span>
          </span>
        </li>
      </ul>

      <div v-if="final.status === 'needs_clarification'" class="supported">
        <p>지원 클럽: {{ final.supported.clubs.join(', ') }}</p>
        <p>지원 포메이션: {{ final.supported.formations.join(', ') }}</p>
        <p>추천 강화: {{ final.supported.reinforcements.map((l) => `${l}강`).join(', ') }}</p>
      </div>

      <div v-if="showSquad && final.squad && final.conditions" class="squad-layout">
        <FormationPitch class="pitch-col" :formation="final.conditions.formation" :rows="final.squad.rows" />
        <div class="facts">
          <dl class="summary">
            <div>
              <dt>총 가격</dt>
              <dd data-testid="summary-price">
                {{ koreanUnits(final.squad.total_price) }} BP
                <span class="exact">{{ groupDigits(final.squad.total_price) }}</span>
              </dd>
            </div>
            <div>
              <dt>총 급여</dt>
              <dd>{{ groupDigits(final.squad.total_salary) }}</dd>
            </div>
            <div>
              <dt>OVR 합</dt>
              <dd>{{ final.squad.total_ovr }}</dd>
            </div>
          </dl>
          <dl class="conditions">
            <div v-for="line in conditionLines(final.conditions)" :key="line.label">
              <dt>{{ line.label }}</dt>
              <dd :title="line.exact ?? undefined">{{ line.value }}</dd>
            </div>
          </dl>
        </div>
      </div>

      <dl v-else-if="final.conditions" class="conditions inline">
        <div v-for="line in conditionLines(final.conditions)" :key="line.label">
          <dt>{{ line.label }}</dt>
          <dd>{{ line.value }}</dd>
        </div>
      </dl>

      <div v-if="coverage" class="coverage" data-testid="coverage">
        <h3>자리별 후보 수</h3>
        <p v-if="emptySlots.length" class="coverage-note">
          후보가 없는 자리: {{ emptySlots.join(', ') }}. 자료에서 이 자리에 놓을 수 있는 후보 카드를 찾지 못했습니다.
        </p>
        <ul>
          <li v-for="c in coverage" :key="c.slot" :class="{ empty: c.count === 0 }">
            <span class="slot">{{ c.slot }}</span>
            <span class="count">{{ c.count.toLocaleString('ko-KR') }}</span>
          </li>
        </ul>
      </div>

      <SquadTable v-if="showSquad && final.squad" :squad="final.squad" />

      <div v-if="final.answer" class="answer">
        <h3>설명</h3>
        <p data-testid="answer">{{ final.answer }}</p>
        <p class="source">
          {{ final.narration_source === 'llm_checked' ? '언어 모델이 쓰고 표의 수치와 대조한 설명입니다.' : '정해진 문장 틀로 만든 설명입니다.' }}
        </p>
      </div>

      <footer class="provenance">
        <p v-if="final.snapshot">
          자료 기준 {{ formatTime(final.snapshot.t0) }}<template v-if="final.snapshot.valid_until">, 유효 기한 {{ formatTime(final.snapshot.valid_until) }}</template>
        </p>
        <p v-if="final.evaluated_at">확인 시각 {{ formatTime(final.evaluated_at) }}</p>
        <p>적용 버전: {{ versionText(final.versions) }}</p>
      </footer>
    </template>
  </section>
</template>

<style scoped>
.result {
  --tone: var(--info);
  display: grid;
  gap: 1rem;
}
.result[data-tone='ok'] {
  --tone: var(--ok);
}
.result[data-tone='warn'] {
  --tone: var(--warn);
}
.result[data-tone='bad'] {
  --tone: var(--bad);
}
.headline {
  font-size: var(--step-2);
  line-height: 1.3;
  font-weight: 700;
  color: var(--tone);
  max-width: 30ch;
  text-wrap: balance;
}
.note {
  color: var(--ink-2);
  max-width: 60ch;
  margin-top: -0.5rem;
}
.detail {
  font-size: var(--step--1);
  color: var(--ink-3);
  overflow-wrap: anywhere;
}
.issues {
  list-style: none;
  margin: 0;
  padding: 0;
  display: grid;
  gap: 0.5rem;
  max-width: 60ch;
}
.issues li {
  display: grid;
  grid-template-columns: 6.5rem 1fr;
  gap: 1rem;
  padding: 0.5rem 0;
  border-top: 1px solid var(--rule);
}
.issue-field {
  font-weight: 700;
  color: var(--warn);
}
.issue-body {
  display: grid;
  gap: 0.125rem;
}
.issue-span {
  font-weight: 500;
  color: var(--ink);
  overflow-wrap: anywhere;
}
.coverage h3 {
  font-size: var(--step-0);
  margin-bottom: 0.25rem;
}
.coverage-note {
  font-size: var(--step--1);
  color: var(--warn);
  margin-bottom: 0.5rem;
  max-width: 60ch;
}
.coverage ul {
  list-style: none;
  margin: 0;
  padding: 0;
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(4.5rem, 1fr));
  gap: 0.25rem;
  max-width: 36rem;
}
.coverage li {
  display: grid;
  gap: 0.125rem;
  padding: 0.375rem 0.5rem;
  border: 1px solid var(--rule);
  border-radius: 4px;
  font-size: var(--step--1);
}
.coverage .slot {
  color: var(--ink-3);
}
.coverage .count {
  font-weight: 700;
  font-variant-numeric: tabular-nums;
}
.coverage li.empty {
  border-color: var(--warn);
  color: var(--warn);
}
.coverage li.empty .slot {
  color: var(--warn);
}
.supported {
  font-size: var(--step--1);
  color: var(--ink-2);
}
.squad-layout {
  display: grid;
  grid-template-columns: minmax(15rem, 22rem) 1fr;
  gap: 2rem;
  align-items: start;
}
.facts {
  display: grid;
  gap: 1.5rem;
}
.summary {
  margin: 0;
  display: grid;
  gap: 0.75rem;
}
.summary dt {
  font-size: var(--step--1);
  color: var(--ink-3);
}
.summary dd {
  margin: 0;
  font-size: var(--step-2);
  font-weight: 700;
  line-height: 1.25;
  overflow-wrap: anywhere;
}
.exact {
  display: block;
  font-size: var(--step--1);
  font-weight: 400;
  color: var(--ink-3);
}
.conditions {
  margin: 0;
  display: grid;
  gap: 0.25rem;
  font-size: var(--step--1);
}
.conditions div {
  display: grid;
  grid-template-columns: 5.5rem 1fr;
  gap: 0.75rem;
}
.conditions dt {
  color: var(--ink-3);
}
.conditions dd {
  margin: 0;
  overflow-wrap: anywhere;
}
.conditions.inline {
  max-width: 32rem;
}
.answer h3 {
  font-size: var(--step-0);
  margin-bottom: 0.25rem;
}
.answer p {
  max-width: 62ch;
}
.answer .source {
  font-size: var(--step--1);
  color: var(--ink-3);
  margin-top: 0.25rem;
}
.provenance {
  border-top: 1px solid var(--rule);
  padding-top: 0.75rem;
  font-size: 0.75rem;
  color: var(--ink-3);
  overflow-wrap: anywhere;
}
@media (max-width: 52rem) {
  .squad-layout {
    grid-template-columns: 1fr;
  }
  .pitch-col {
    max-width: 22rem;
  }
}
</style>
