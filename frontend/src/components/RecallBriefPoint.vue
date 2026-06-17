<script setup lang="ts">
import { computed } from "vue";
import type { Row } from "../api";

const props = withDefaults(
  defineProps<{ point: Row; hits?: Row[]; variant?: "item" | "warn" }>(),
  { variant: "item" },
);

// Cited versions are validated server-side to sit within the retrieved hits, so the
// shown page's hits carry the post_id needed to link a citation back to its evidence.
function postForVersion(versionId: number): number | null {
  const hits = Array.isArray(props.hits) ? props.hits : [];
  const hit = hits.find((item) => Number(item.version_id) === Number(versionId));
  return hit ? Number(hit.post_id) : null;
}

const versionIds = computed<number[]>(() =>
  Array.isArray(props.point.version_ids) ? (props.point.version_ids as number[]) : [],
);

// A warning is lifted into a callout <div>; a normal point stays an <li>. Either way the
// citation chain travels with it, so even a 样本少 warning can still be traced to its
// evidence versions.
const rootTag = computed(() => (props.variant === "warn" ? "div" : "li"));
const rootClass = computed(() => (props.variant === "warn" ? "brief-warn" : "brief-point"));
</script>

<template>
  <component :is="rootTag" :class="rootClass">
    <span class="brief-text"><template v-if="variant === 'warn'">⚠ </template>{{ point.text }}</span>
    <!-- Citation chain folds away by default: the date + text is the act; the v-ids are
         provenance you open only to audit. Summary keeps the date visible while collapsed. -->
    <details v-if="versionIds.length" class="brief-cites">
      <summary>
        <span v-if="point.date_label" class="brief-date">{{ point.date_label }}</span>
        <span class="muted small">引用 {{ versionIds.length }} 条</span>
      </summary>
      <span class="brief-cite-list">
        <template v-for="vid in versionIds" :key="vid">
          <a v-if="postForVersion(vid)" :href="`/posts/${postForVersion(vid)}`" class="brief-cite">v{{ vid }}</a>
          <span v-else class="brief-cite">v{{ vid }}</span>
        </template>
      </span>
    </details>
  </component>
</template>
