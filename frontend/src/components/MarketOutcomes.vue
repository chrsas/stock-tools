<script setup lang="ts">
import type { Row } from "../api";
import MarketChart from "./MarketChart.vue";

defineProps<{ outcomes?: Row[]; snapshot?: Row | null }>();

function percent(value: unknown): string {
  return value == null ? "无" : `${Number(value) >= 0 ? "+" : ""}${(Number(value) * 100).toFixed(2)}%`;
}
</script>

<template>
  <div v-if="snapshot" class="market-snapshot">
    <span class="eyebrow">发言后市场变化</span>
    <strong :class="{ positive: snapshot.excess_return > 0, negative: snapshot.excess_return < 0 }">
      超额 {{ percent(snapshot.excess_return) }}
    </strong>
    <span>
      标的 {{ percent(snapshot.raw_return) }} · {{ snapshot.benchmark_ticker }}
      {{ percent(snapshot.benchmark_return) }}
    </span>
    <small>{{ snapshot.start_date }} 至 {{ snapshot.end_date }} · 描述性共同收盘口径</small>
    <MarketChart :series="snapshot.series" :benchmark-ticker="snapshot.benchmark_ticker" />
  </div>
  <div v-for="outcome in outcomes" :key="outcome.claim_id" class="market-row">
    <strong>{{ outcome.ticker }} · {{ outcome.direction }} · {{ outcome.horizon_days == null ? "未设期限" : `${outcome.horizon_days} 天` }}</strong>
    <span v-if="outcome.resolved_at">
      标的变化 {{ percent(outcome.raw_return) }} · 基准变化 {{ percent(outcome.benchmark_return) }} ·
      <b :class="{ positive: outcome.excess_return > 0, negative: outcome.excess_return < 0 }">超额变化 {{ percent(outcome.excess_return) }}</b>
    </span>
    <span v-else class="muted">等待结果</span>
  </div>
</template>
