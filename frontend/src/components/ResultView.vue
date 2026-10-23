<script setup lang="ts">
import { computed } from 'vue';
import type { Conditions, FinalStatus } from '../contracts/webAgentV1';
import { isFinalV2, MULTI_FACTOR, scoreText, type FinalResultV2, type ObjectiveStage, type StageName, type StageStatus } from '../contracts/webAgentV2';
import type { AnyFinalResult, Outcome } from '../services/agentSession';
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

/** 스트림 error 이벤트의 공개 코드별 제목. 안내 문장은 서버가 보낸 message를 그대로 쓴다. */
const SELECTION_CHECK = '모델의 조건 선택을 검증하지 못해 추천하지 않았습니다';
const ERROR_TITLES: Record<string, string> = {
  selection_provider_error: '조건 추출 모델을 사용할 수 없어 요청을 처리하지 못했습니다',
  request_deadline: '응답 기한 안에 추천을 끝내지 못했습니다',
  invalid_selection: SELECTION_CHECK,
  candidate_set_mismatch: SELECTION_CHECK,
  invalid_context_selection: SELECTION_CHECK,
  unknown_candidate: SELECTION_CHECK,
  duplicate_candidate: SELECTION_CHECK,
  field_mismatch: SELECTION_CHECK,
  incomplete_coverage: SELECTION_CHECK,
  invalid_registry: SELECTION_CHECK,
};

/** 다요소 방식 성공 결과의 상태 설명. OVR 방식 문구(OVR 합 최대)를 다요소 결과에 쓰지 않는다. */
const MULTI_NOTE: Partial<Record<FinalStatus, string>> = {
  optimal: '필드 OVR → GK OVR → 실험 다요소 점수 순서의 단계 목표를 확인한 조합입니다. 실험 점수이며 실제 경기 성능을 보장하지 않습니다.',
  feasible: '조건은 모두 지켰지만 일부 단계의 최적성은 제한 시간 안에 확인하지 못했습니다. 아래 단계 요약을 확인해 주세요.',
};

const STAGE_LABELS: Record<StageName, string> = {
  outfield_ovr: '필드 OVR 최대',
  gk_ovr: 'GK OVR',
  multi_factor: '다요소 합',
};

const STAGE_STATUS: Record<StageStatus, string> = {
  optimal: '최적 확인',
  incomplete: '시간 내 미확정',
  not_run: '실행 안 함',
};

/** 409 objective_unavailable: 고른 방식을 실행 전에 거절한 경우. 다른 방식으로 바꿔 다시 보내지 않는다. */
const OBJECTIVE_UNAVAILABLE = {
  title: '고른 추천 방식을 지금 서버에서 사용할 수 없습니다',
  note: '서버가 실행 전에 요청을 거절했습니다. OVR 방식으로 바꾸거나 자동으로 다시 보내지 않습니다. 방식 목록을 다시 확인한 뒤 사용 가능한 방식을 골라 새로 보내 주세요.',
  tone: 'warn' as Tone,
};

/** 스트림을 열기 전 HTTP 거절. 본문은 해석하지 않고 상태 코드만으로 안내한다(INT-020/021). */
const HTTP_COPY: Record<number, { title: string; note: string; tone: Tone }> = {
  409: {
    title: '다른 추천 요청이 진행 중입니다',
    note: '서버는 한 번에 한 요청만 처리합니다. 진행 중인 요청이 끝난 뒤 다시 보내 주세요. 자동으로 다시 보내지 않습니다.',
    tone: 'warn',
  },
  413: {
    title: '요청 본문이 서버 한도를 넘었습니다',
    note: '요청 문장을 줄여 다시 보내 주세요.',
    tone: 'warn',
  },
  422: {
    title: '서버가 요청 형식을 받지 않았습니다',
    note: '조건 해석 결과가 아니라 요청 자체가 거절된 것입니다. 새로고침 뒤에도 반복되면 웹과 서버의 계약 버전을 확인해야 합니다.',
    tone: 'bad',
  },
  403: {
    title: '이 화면의 주소가 서버 허용 목록에 없습니다',
    note: 'http://localhost:5173 또는 http://127.0.0.1:5173 개발 서버에서 연 화면만 요청을 보낼 수 있습니다.',
    tone: 'bad',
  },
  408: {
    title: '요청을 기한 안에 다 보내지 못했습니다',
    note: '네트워크 상태를 확인한 뒤 요청을 새로 보내 주세요.',
    tone: 'bad',
  },
  415: {
    title: '서버가 JSON이 아닌 요청을 거절했습니다',
    note: '요청이 JSON으로 전송되지 않았습니다. 웹과 서버의 계약 버전을 확인해야 합니다.',
    tone: 'bad',
  },
  503: {
    title: '추천 서버가 아직 준비되지 않았습니다',
    note: '모델 확인이 끝나지 않았거나 자료 기한이 지난 상태일 수 있습니다. 서버 준비 뒤 다시 보내 주세요.',
    tone: 'bad',
  },
};

const final = computed<AnyFinalResult | null>(() => (props.outcome.kind === 'final' ? props.outcome.result : null));
const v2 = computed<FinalResultV2 | null>(() => (final.value && isFinalV2(final.value) ? final.value : null));
const multi = computed(() => v2.value?.objective.id === MULTI_FACTOR);
const showSquad = computed(() => final.value?.squad != null && !props.expired);

/** 단계 값: OVR 단계는 정수, 다요소 단계는 정수 점수의 정확한 두 자리 표기(반올림 없음). */
function stageValue(stage: ObjectiveStage): string {
  if (stage.value === null) return '—';
  return stage.name === 'multi_factor' ? `${scoreText(stage.value)}점` : String(stage.value);
}

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
      const copy = STATUS_COPY[o.result.status];
      if (isFinalV2(o.result) && o.result.objective.id === MULTI_FACTOR && MULTI_NOTE[o.result.status]) {
        return { ...copy, note: MULTI_NOTE[o.result.status]! };
      }
      if (o.result.data_mode === 'observed' && o.result.status === 'optimal') {
        return { ...copy, note: '관측 자료와 조건에서 OVR 합이 가장 높은 조합입니다. 실제 경기 성능을 보장하지 않습니다.' };
      }
      if (o.result.data_mode === 'observed' && o.result.status === 'infeasible') {
        return { ...copy, note: '보유한 관측 자료 기준의 결과입니다. 수집하지 않은 클럽이나 누락된 자료가 있을 수 있으며, 게임 전체에서 불가능하다는 뜻은 아닙니다.' };
      }
      return copy;
    case 'server_error':
      return { title: ERROR_TITLES[o.code] ?? '추천 중 오류가 발생했습니다', note: o.message, tone: 'bad', detail: `오류 코드 ${o.code}` };
    case 'protocol_error':
      return {
        title: '서버 응답이 약속한 형식과 달라 결과를 표시하지 않습니다',
        note: '다시 시도하려면 요청을 새로 보내 주세요.',
        tone: 'bad',
        detail: o.detail,
      };
    case 'transport_error': {
      if (o.status === 409 && o.code === 'objective_unavailable') {
        return { ...OBJECTIVE_UNAVAILABLE, detail: 'HTTP 409 objective_unavailable' };
      }
      const http = o.status === undefined ? undefined : HTTP_COPY[o.status];
      if (http) return { ...http, detail: `HTTP ${o.status}` };
      return { title: '서버와 통신하지 못했습니다', note: o.detail, tone: 'bad' };
    }
    case 'aborted':
      return { title: '요청 읽기를 멈췄습니다', note: '서버 계산이 함께 취소되었는지는 알 수 없습니다.', tone: 'warn' };
  }
  return { title: '', note: null, tone: 'info' };
});

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
      <p class="note" data-testid="result-data-mode">
        {{ final.data_mode === 'observed' ? '넥슨 데이터센터 관측 자료 · 가격은 참고가격이며 거래를 보장하지 않습니다.' : '합성 가상 리그 자료 · 선수 이름과 수치는 실제와 무관합니다.' }}
      </p>
      <div v-if="v2" class="objective" data-testid="result-objective">
        <p>
          <span class="label">추천 방식</span>
          <template v-if="multi">
            필드 우선·GK 단계·다요소 보조 <span class="badge">실험</span>
          </template>
          <template v-else>기존 OVR</template>
        </p>
        <p v-if="v2.objective.policy" class="policy" data-testid="result-policy">
          정책 {{ v2.objective.policy.policy_id }}
          <span class="quiet" :title="v2.objective.policy.version">{{ v2.objective.policy.version.slice(0, 12) }}</span>
        </p>
      </div>
      <ul v-if="final.issues.length" class="issues" data-testid="issues">
        <li v-for="(issue, i) in final.issues" :key="i">
          <span class="issue-field">{{ FIELD_LABELS[issue.field] ?? issue.field }}</span>
          <span>{{ issue.message }}</span>
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
              <dd data-testid="summary-ovr">{{ final.squad.total_ovr }}</dd>
            </div>
            <div v-if="v2?.squad" class="split">
              <dt>필드 OVR · GK OVR</dt>
              <dd data-testid="summary-ovr-split">{{ v2.squad.outfield_ovr }} · {{ v2.squad.gk_ovr }}</dd>
            </div>
            <div v-if="multi && v2?.squad?.total_score">
              <dt>다요소 점수 합 (실험)</dt>
              <dd data-testid="summary-score">{{ v2.squad.total_score }}</dd>
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

      <section v-if="showSquad && v2 && v2.objective.stages.length" class="stages" data-testid="stages">
        <h3>단계 요약</h3>
        <ol>
          <li v-for="stage in v2.objective.stages" :key="stage.name" :data-stage="stage.name" :data-status="stage.status">
            <span class="stage-name">{{ STAGE_LABELS[stage.name] }}</span>
            <span class="stage-status">{{ STAGE_STATUS[stage.status] }}</span>
            <span class="stage-value">{{ stageValue(stage) }}</span>
          </li>
        </ol>
        <p class="quiet">단계 값은 그 단계에서 확인한 목표값입니다. 다음 단계의 허용폭 때문에 최종 표의 합과 다를 수 있습니다.</p>
      </section>

      <SquadTable v-if="showSquad && final.squad" :squad="final.squad" :scores="multi" />

      <div v-if="final.answer" class="answer">
        <h3>설명</h3>
        <p data-testid="answer">{{ final.answer }}</p>
        <p class="source">
          {{ final.narration_source === 'llm_checked' ? '언어 모델이 쓰고 표의 수치와 대조한 설명입니다.' : '정해진 문장 틀로 만든 설명입니다.' }}
        </p>
      </div>

      <footer class="provenance">
        <p v-if="final.snapshot">
          요청 기준 {{ formatTime(final.snapshot.t0) }}<template v-if="final.snapshot.valid_until">, 유효 기한 {{ formatTime(final.snapshot.valid_until) }}</template>
        </p>
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
.objective {
  display: grid;
  gap: 0.125rem;
  font-size: var(--step--1);
}
.objective .label {
  color: var(--ink-3);
  margin-right: 0.5rem;
}
.badge {
  border: 1px solid var(--warn);
  color: var(--warn);
  border-radius: 3px;
  padding: 0 0.3rem;
  font-size: 0.75rem;
  font-weight: 700;
}
.policy,
.quiet {
  color: var(--ink-3);
  overflow-wrap: anywhere;
}
.summary .split dd {
  font-size: var(--step-1);
}
.stages {
  display: grid;
  gap: 0.375rem;
  font-size: var(--step--1);
}
.stages h3 {
  font-size: var(--step-0);
}
.stages ol {
  margin: 0;
  padding: 0;
  list-style: none;
  display: grid;
  gap: 0.25rem;
  max-width: 32rem;
}
.stages li {
  display: grid;
  grid-template-columns: 7rem 7rem 1fr;
  gap: 0.5rem;
  padding: 0.25rem 0;
  border-top: 1px solid var(--rule);
}
.stages li[data-status='incomplete'] .stage-status,
.stages li[data-status='not_run'] .stage-status {
  color: var(--warn);
}
.stage-value {
  text-align: right;
  font-variant-numeric: tabular-nums;
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
