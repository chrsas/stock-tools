<script setup lang="ts">
import type { Row } from "../api";
import { originalUrl } from "../format";

defineProps<{ item: Row; evidence?: boolean }>();
</script>

<template>
  <div class="links">
    <a v-if="originalUrl(item)" :href="originalUrl(item)" target="_blank" rel="noopener noreferrer">打开雪球原帖</a>
    <a v-if="evidence" :href="`/posts/${item.post_id || item.id}`">打开证据卡</a>
    <a v-if="item.author_platform_uid" :href="`/authors/${encodeURIComponent(item.author_platform_uid)}`">本地作者页</a>
    <a v-if="item.author_platform_uid" :href="`https://xueqiu.com/u/${encodeURIComponent(item.author_platform_uid)}`" target="_blank" rel="noopener noreferrer">雪球主页</a>
  </div>
</template>
