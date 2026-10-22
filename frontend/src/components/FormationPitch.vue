<script setup lang="ts">
import type { SquadRow } from '../contracts/webAgentV1';

/**
 * 검증된 11명을 공격 방향이 위쪽인 경기장에 배치해 보여 준다. 화면 오른쪽이 선수의 오른쪽이다.
 * 좌표는 표시용이며 배치 판단이나 계산에 쓰지 않는다.
 */
defineProps<{ formation: string; rows: SquadRow[] }>();

const SPOTS: Record<string, [number, number]> = {
  GK: [50, 87],
  RCB: [63, 76],
  LCB: [37, 76],
  RB: [86, 69],
  LB: [14, 69],
  CDM: [50, 60],
  RDM: [64, 58],
  LDM: [36, 58],
  RCM: [68, 45],
  LCM: [32, 45],
  RM: [86, 41],
  LM: [14, 41],
  CAM: [50, 33],
  RAM: [80, 30],
  LAM: [20, 30],
  RW: [82, 17],
  LW: [18, 17],
  ST: [50, 11],
  RS: [63, 12],
  LS: [37, 12],
};

function style(slot: string) {
  const [x, y] = SPOTS[slot] ?? [50, 50];
  return { left: `${x}%`, top: `${y}%` };
}
</script>

<template>
  <figure class="pitch-figure">
    <div class="pitch" role="img" :aria-label="`${formation} 배치도. 자세한 수치는 아래 표에 있습니다.`">
      <div class="line halfway" />
      <div class="circle" />
      <div class="box top" />
      <div class="box bottom" />
      <div class="six top" />
      <div class="six bottom" />
      <div v-for="row in rows" :key="row.slot" class="player" :style="style(row.slot)" aria-hidden="true">
        <span class="dot">{{ row.reinforce_level }}</span>
        <span class="name">{{ row.name }}</span>
        <span class="slot">{{ row.slot }} {{ row.raw_ovr }}</span>
      </div>
    </div>
    <figcaption>{{ formation }}. 원 안의 숫자는 강화 단계, 자리 옆 숫자는 OVR입니다.</figcaption>
  </figure>
</template>

<style scoped>
.pitch-figure {
  margin: 0;
}
.pitch {
  position: relative;
  aspect-ratio: 68 / 100;
  width: 100%;
  border-radius: 6px;
  overflow: hidden;
  background: repeating-linear-gradient(
    to bottom,
    var(--pitch) 0,
    var(--pitch) 10%,
    var(--pitch-stripe) 10%,
    var(--pitch-stripe) 20%
  );
  color: var(--chalk);
  --line: rgba(238, 244, 239, 0.45);
}
.pitch::before {
  content: '';
  position: absolute;
  inset: 3%;
  border: 1.5px solid var(--line);
}
.halfway {
  position: absolute;
  left: 3%;
  right: 3%;
  top: 50%;
  border-top: 1.5px solid var(--line);
}
.circle {
  position: absolute;
  left: 50%;
  top: 50%;
  width: 26%;
  aspect-ratio: 1;
  border: 1.5px solid var(--line);
  border-radius: 50%;
  transform: translate(-50%, -50%);
}
.box,
.six {
  position: absolute;
  left: 50%;
  transform: translateX(-50%);
  border: 1.5px solid var(--line);
}
.box {
  width: 58%;
  height: 15%;
}
.six {
  width: 26%;
  height: 5.5%;
}
.box.top,
.six.top {
  top: 3%;
  border-top: 0;
}
.box.bottom,
.six.bottom {
  bottom: 3%;
  border-bottom: 0;
}
.player {
  position: absolute;
  transform: translate(-50%, -50%);
  display: grid;
  justify-items: center;
  text-align: center;
  width: 6.5rem;
  line-height: 1.25;
}
.dot {
  display: grid;
  place-items: center;
  width: 1.75rem;
  height: 1.75rem;
  border-radius: 50%;
  background: var(--chalk);
  color: var(--pitch);
  font-weight: 700;
  font-size: var(--step--1);
}
.name {
  margin-top: 0.2rem;
  font-size: 0.75rem;
  font-weight: 500;
  max-width: 100%;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.slot {
  font-size: 0.6875rem;
  opacity: 0.8;
}
figcaption {
  margin-top: 0.5rem;
  font-size: var(--step--1);
  color: var(--ink-3);
}
@media (max-width: 30rem) {
  .player {
    width: 4.5rem;
  }
  .name {
    font-size: 0.6875rem;
  }
  .slot {
    display: none;
  }
}
</style>
