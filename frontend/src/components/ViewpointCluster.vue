<script setup lang="ts">
import type { Row } from "../api";
import { fmtTime, postTitle } from "../format";
import MarketOutcomes from "./MarketOutcomes.vue";
import PostLinks from "./PostLinks.vue";

defineProps<{ cluster: Row }>();
</script>

<template>
  <article class="card">
    <header><h2>{{ cluster.title }}</h2><span class="pill">{{ cluster.statement_count }} 次相关发言</span></header>
    <p class="muted">首次记录 {{ fmtTime(cluster.first_at) }} · 最近强化 {{ fmtTime(cluster.latest_at) }}</p>
    <p>最新依据「{{ cluster.viewpoints?.[0]?.enrichment_evidence_snippet || "无依据片段" }}」</p>
    <MarketOutcomes :outcomes="cluster.viewpoints?.[0]?.market_outcomes" />
    <details>
      <summary>展开 {{ cluster.statement_count }} 条相关发言</summary>
      <section v-for="viewpoint in cluster.viewpoints" :key="viewpoint.post_id" class="statement">
        <h3><a :href="`/posts/${viewpoint.post_id}`">{{ postTitle(viewpoint) }}</a></h3>
        <MarketOutcomes :outcomes="viewpoint.market_outcomes" />
        <PostLinks :item="viewpoint" />
        <pre>{{ viewpoint.current_text }}</pre>
      </section>
    </details>
  </article>
</template>
