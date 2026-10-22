<script setup lang="ts">
import type { Squad } from '../contracts/webAgentV1';
import { formatScore, groupDigits } from '../utils/format';

/** 서버가 확정한 행과 합계를 그대로 표시한다. 정렬·재계산하지 않는다. */
defineProps<{ squad: Squad }>();
</script>

<template>
  <div class="table-wrap" tabindex="0" role="region" aria-label="스쿼드 표, 좁은 화면에서는 가로로 스크롤됩니다">
    <table>
      <caption class="visually-hidden">검증된 스쿼드 11명</caption>
      <thead>
        <tr>
          <th scope="col">자리</th>
          <th scope="col">선수</th>
          <th scope="col" class="num">강화</th>
          <th scope="col" class="num">OVR</th>
          <th scope="col" class="num">성능</th>
          <th scope="col" class="num">가격 (BP)</th>
          <th scope="col" class="num">급여</th>
          <th scope="col">배치</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in squad.rows" :key="row.slot" data-testid="squad-row">
          <th scope="row">{{ row.slot }}</th>
          <td class="name">
            {{ row.name }}
            <span class="id">spid {{ row.spid }}</span>
          </td>
          <td class="num">{{ row.reinforce_level }}강</td>
          <td class="num">{{ row.raw_ovr }}</td>
          <td class="num">{{ formatScore(row.performance) }}</td>
          <td class="num" data-testid="row-price">{{ groupDigits(row.price) }}</td>
          <td class="num" data-testid="row-salary">{{ groupDigits(row.salary) }}</td>
          <td>{{ row.placement_type === 'base' ? '기본' : row.placement_type }}</td>
        </tr>
      </tbody>
      <tfoot>
        <tr>
          <th scope="row" colspan="3">합계</th>
          <td class="num" data-testid="total-ovr">{{ squad.total_ovr }}</td>
          <td class="num">{{ formatScore(squad.performance) }}</td>
          <td class="num" data-testid="total-price">{{ groupDigits(squad.total_price) }}</td>
          <td class="num" data-testid="total-salary">{{ groupDigits(squad.total_salary) }}</td>
          <td />
        </tr>
      </tfoot>
    </table>
  </div>
</template>

<style scoped>
.table-wrap {
  overflow-x: auto;
  -webkit-overflow-scrolling: touch;
}
table {
  width: 100%;
  border-collapse: collapse;
  font-size: var(--step--1);
  white-space: nowrap;
}
th,
td {
  padding: 0.5rem 0.75rem 0.5rem 0;
  text-align: left;
  border-bottom: 1px solid var(--rule);
  vertical-align: baseline;
}
thead th {
  color: var(--ink-3);
  font-weight: 500;
}
tbody th {
  font-weight: 700;
}
.num {
  text-align: right;
}
.name {
  font-weight: 500;
}
.id {
  display: block;
  font-weight: 400;
  font-size: 0.75rem;
  color: var(--ink-3);
}
tfoot th,
tfoot td {
  border-bottom: 0;
  border-top: 2px solid var(--ink);
  font-weight: 700;
}
</style>
