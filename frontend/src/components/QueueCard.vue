<script setup lang="ts">
import type { Row } from "../api";
import { fmtTime, postTitle } from "../format";
import AuthorBadge from "./AuthorBadge.vue";
import PostLinks from "./PostLinks.vue";

defineProps<{ item: Row; pinned: boolean }>();
defineEmits<{ action: [path: string] }>();
</script>

<template>
  <article class="card">
    <header>
      <AuthorBadge :item="item" />
      <strong>{{ postTitle(item) }}</strong>
    </header>
    <div class="pills">
      <span v-if="item.label_first_hand_info" class="lbl lbl-first">第一手信息</span>
      <span v-if="item.label_transferable_framework" class="lbl lbl-frame">可迁移框架</span>
      <span v-if="item.label_reasoned_non_consensus" class="lbl lbl-reason">有据非共识</span>
      <span>{{ item.post_type }}</span>
    </div>
    <pre class="queue-preview">{{ item.current_text || "暂无完整正文版本" }}</pre>
    <blockquote v-if="item.enrichment_evidence_snippet">{{ item.enrichment_evidence_snippet }}</blockquote>
    <div class="actions">
      <button v-if="pinned" class="secondary" @click="$emit('action', `/posts/${item.post_id}/unpin`)">取消钉住</button>
      <button v-else @click="$emit('action', `/posts/${item.post_id}/pin`)">钉住当前版本</button>
      <PostLinks :item="item" evidence />
    </div>
    <details>
      <summary>证据详情</summary>
      <div class="facts">
        <span><b>当前版本观察</b>{{ fmtTime(item.current_version_first_observed_at) }}</span>
        <span><b>内容版本</b>{{ item.version_count || 0 }} 个</span>
        <span><b>证据来源</b>{{ item.latest_evidence_channel || "未知" }} run {{ item.latest_evidence_run_id || "无" }}</span>
      </div>
      <pre>{{ item.current_text || "暂无完整正文版本" }}</pre>
    </details>
  </article>
</template>
