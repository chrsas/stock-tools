<script setup lang="ts">
import type { Row } from "../api";
import { fmtTime, postTitle } from "../format";
import AuthorBadge from "./AuthorBadge.vue";
import PostLinks from "./PostLinks.vue";

defineProps<{ item: Row; showLabels?: boolean }>();
</script>

<template>
  <article class="card">
    <header>
      <AuthorBadge :item="item" />
      <a :href="`/posts/${item.post_id}`">{{ postTitle(item) }}</a>
    </header>
    <div v-if="showLabels" class="pills">
      <span v-if="item.label_first_hand_info">第一手信息</span>
      <span v-if="item.label_transferable_framework">可迁移框架</span>
      <span v-if="item.label_reasoned_non_consensus">有据非共识</span>
      <span>{{ item.post_type }}</span>
    </div>
    <p>{{ item.status?.human_label }}</p>
    <p class="muted">{{ item.status?.deletion_signal_label }}</p>
    <p class="muted">首次观察 {{ fmtTime(item.first_seen_at) }} · 最后在场 {{ fmtTime(item.last_present_at) }}</p>
    <PostLinks :item="item" evidence />
    <pre>{{ item.current_text || "暂无完整正文版本" }}</pre>
  </article>
</template>
