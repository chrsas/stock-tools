<script setup lang="ts">
import type { Row } from "../api";
import { authorName, avatarUrl } from "../format";

defineProps<{ item: Row }>();
</script>

<template>
  <div class="author-badge">
    <img v-if="avatarUrl(item.author_avatar_url)" :src="avatarUrl(item.author_avatar_url)" alt="">
    <span v-else class="avatar-placeholder"></span>
    <span>
      <strong>{{ authorName(item) }}</strong>
      <small>uid {{ item.author_platform_uid }}</small>
    </span>
  </div>
</template>
