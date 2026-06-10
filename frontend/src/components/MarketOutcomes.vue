<script setup lang="ts">
import type { Row } from "../api";

defineProps<{ outcomes?: Row[] }>();

function percent(value: unknown): string {
  return value == null ? "无" : `${Number(value) >= 0 ? "+" : ""}${(Number(value) * 100).toFixed(2)}%`;
}
</script>

<template>
  <p v-if="!outcomes?.length" class="muted">尚未提取可证伪命题，暂时无法关联市场变化。</p>
  <div v-for="outcome in outcomes" :key="outcome.claim_id" class="market-row">
    <strong>{{ outcome.ticker }} · {{ outcome.direction }} · {{ outcome.horizon_days == null ? "未设期限" : `${outcome.horizon_days} 天` }}</strong>
    <span v-if="outcome.resolved_at">
      标的变化 {{ percent(outcome.raw_return) }} · 基准变化 {{ percent(outcome.benchmark_return) }} ·
      <b :class="{ positive: outcome.excess_return > 0, negative: outcome.excess_return < 0 }">超额变化 {{ percent(outcome.excess_return) }}</b>
    </span>
    <span v-else class="muted">等待结果</span>
  </div>
</template>
