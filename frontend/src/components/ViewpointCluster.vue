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
    <p class="cluster-rationale">
      <b>{{ cluster.viewpoints?.[0]?.enrichment_stance_summary ? "立场摘要" : "富化判断" }}</b>
      {{ cluster.viewpoints?.[0]?.enrichment_stance_summary || cluster.viewpoints?.[0]?.enrichment_rationale || "暂无富化判断" }}
    </p>
    <MarketOutcomes :snapshot="cluster.market_snapshot" :outcomes="cluster.viewpoints?.[0]?.market_outcomes" />
    <blockquote v-if="cluster.viewpoints?.[0]?.enrichment_evidence_snippet">
      {{ cluster.viewpoints[0].enrichment_evidence_snippet }}
    </blockquote>
    <details>
      <summary>展开 {{ cluster.statement_count }} 条相关发言</summary>
      <section v-for="viewpoint in cluster.viewpoints" :key="viewpoint.post_id" class="statement">
        <h3><a :href="`/posts/${viewpoint.post_id}`">{{ postTitle(viewpoint) }}</a></h3>
        <p v-if="viewpoint.source_state === 'gone_confirmed'" class="error">原帖已不可见，证据见版本 {{ viewpoint.version_id }}</p>
        <MarketOutcomes :outcomes="viewpoint.market_outcomes" />
        <PostLinks :item="viewpoint" />
        <pre>{{ viewpoint.current_text }}</pre>
      </section>
    </details>
  </article>
</template>
