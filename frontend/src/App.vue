<script setup lang="ts">
import { onMounted, ref } from "vue";
import { loadPage, mutate, type Row } from "./api";
import { authorName, fmtTime, postTitle, xueqiuUrl } from "./format";
import AuthorBadge from "./components/AuthorBadge.vue";
import PostLinks from "./components/PostLinks.vue";
import QueueCard from "./components/QueueCard.vue";
import TimelineCard from "./components/TimelineCard.vue";
import ViewpointCluster from "./components/ViewpointCluster.vue";

const page = ref<Row | null>(null);
const error = ref("");
const busy = ref(false);
const theme = ref(localStorage.getItem("kol-theme") || "system");

function applyTheme() {
  document.documentElement.dataset.theme = theme.value === "system"
    ? matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light"
    : theme.value;
  localStorage.setItem("kol-theme", theme.value);
}

async function refresh() {
  busy.value = true;
  error.value = "";
  try { page.value = await loadPage(); }
  catch (reason) { error.value = String(reason); }
  finally { busy.value = false; }
}

async function action(path: string, values: Row = {}) {
  if (!page.value) return;
  busy.value = true;
  try {
    await mutate(path, page.value.csrf_token, values);
    await refresh();
  } catch (reason) {
    error.value = String(reason);
    busy.value = false;
  }
}

function submitAttention(event: Event) {
  if (!page.value) return;
  const form = event.currentTarget as HTMLFormElement;
  const values = new FormData(form);
  action(`/posts/${page.value.card.post.id}/attention`, {
    version_id: page.value.card.post.current_version_id,
    reason: String(values.get("reason") || ""),
    expectation: String(values.get("expectation") || ""),
  });
}

function hasMarketFeedback(clusters: Row[]): boolean {
  return clusters.some((cluster) => cluster.market_snapshot
    || cluster.viewpoints?.some((viewpoint: Row) => viewpoint.market_outcomes?.length));
}

function navActive(view: string): boolean {
  const current = page.value?.view;
  if (!current) return false;
  if (view === "authors") return current === "authors" || current === "author";
  return current === view;
}

onMounted(() => { applyTheme(); refresh(); });
</script>

<template>
  <div class="shell">
    <nav class="sidebar">
      <a class="logo" href="/">
        <span class="logo-mark">照</span>
        <span class="logo-text"><strong>KOL 照妖镜</strong><small class="muted">市场观点核验终端</small></span>
      </a>
      <ul class="nav">
        <li><a class="nav-item" :class="{ on: navActive('authors') }" href="/"><svg viewBox="0 0 24 24" class="ico"><path d="M3 12h4l2 6 4-14 2 8h6" /></svg>博主观点</a></li>
        <li><a class="nav-item" :class="{ on: navActive('queue') }" href="/?view=queue"><svg viewBox="0 0 24 24" class="ico"><path d="M3 12h5l2 3h4l2-3h5" /><path d="M5 5h14v14H5z" /></svg>待处理队列</a></li>
        <li><a class="nav-item" :class="{ on: navActive('pinned') }" href="/?view=pinned"><svg viewBox="0 0 24 24" class="ico"><path d="M12 17v5" /><path d="M9 3h6l-1 6 3 3v2H7v-2l3-3-1-6z" /></svg>已钉住</a></li>
        <li><a class="nav-item" :class="{ on: navActive('raw') }" href="/?view=raw"><svg viewBox="0 0 24 24" class="ico"><path d="M4 7h16" /><path d="M4 12h16" /><path d="M4 17h10" /></svg>原始时间线</a></li>
        <li><a class="nav-item" :class="{ on: navActive('filtered') }" href="/?view=filtered"><svg viewBox="0 0 24 24" class="ico"><path d="M4 5h16l-6 7v6l-4 2v-8z" /></svg>标签过滤流</a></li>
      </ul>
      <div class="sidebar-foot">
        <span class="eyebrow">prompt 版本</span>
        <small class="muted mono">{{ page?.prompt_version || "enrich-v1" }} · 描述性共同收盘 v1</small>
      </div>
    </nav>

    <div class="frame">
      <header class="topbar">
        <div class="topbar-tape">
          <span class="dot-live" aria-hidden="true"></span>
          <span class="muted small">红涨绿跌 · A股口径</span>
        </div>
        <select v-model="theme" aria-label="主题" @change="applyTheme">
          <option value="system">跟随系统</option><option value="light">浅色</option><option value="dark">暗色</option>
        </select>
      </header>

      <main class="content">
        <p v-if="busy" class="notice">正在读取归档...</p>
        <p v-if="error" class="error">{{ error }}</p>

        <template v-if="page?.view === 'authors'">
          <div class="page-title"><div><h1>博主最近观点</h1><p class="sub">选择博主，查看最近市场相关观点和后续变化。</p></div></div>
          <div class="author-layout">
            <aside class="panel roster">
              <div class="roster-head"><span class="eyebrow">博主</span><small class="muted">{{ page.authors.length }} 位在档</small></div>
              <div v-for="author in page.authors" :key="author.author_platform_uid" class="author-option" :class="{ active: author.author_platform_uid === page.selected?.author_platform_uid }">
                <a class="author-pick" :href="`/?author=${encodeURIComponent(author.author_platform_uid)}`">
                  <AuthorBadge :item="author" />
                  <small class="muted">观点发言 {{ author.viewpoint_count }} · 已评估观点 {{ author.evaluated_viewpoint_count }}</small>
                </a>
                <a v-if="xueqiuUrl(author)" class="xq-jump" :href="xueqiuUrl(author)" target="_blank" rel="noopener noreferrer" title="在雪球查看主页">雪球 ↗</a>
              </div>
              <p class="roster-note muted">仅展示观点构成，不做跨博主排名或命中率评分。</p>
            </aside>
            <section class="stream">
              <div v-if="page.selected" class="author-banner">
                <AuthorBadge :item="page.selected" />
                <a v-if="xueqiuUrl(page.selected)" class="xq-jump" :href="xueqiuUrl(page.selected)" target="_blank" rel="noopener noreferrer" title="在雪球查看主页">雪球主页 ↗</a>
              </div>
              <div class="stream-label"><span class="eyebrow">最近 {{ page.clusters.length }} 个观点簇</span></div>
              <p v-if="page.clusters.length && !hasMarketFeedback(page.clusters)" class="empty soft">
                尚未导入可用行情或记录市场结果，当前先展示观点证据。
              </p>
              <ViewpointCluster v-for="cluster in page.clusters" :key="cluster.title + cluster.latest_at" :cluster="cluster" />
              <p v-if="!page.clusters.length" class="empty">最近还没有具备明确市场关联的观点发言。</p>
            </section>
          </div>
        </template>

        <template v-else-if="page?.view === 'queue' || page?.view === 'pinned'">
          <div class="page-title"><div><h1>{{ page.view === "pinned" ? "已钉住" : "待处理注意力" }}</h1><p class="sub">围绕证据处置高信号版本。</p></div></div>
          <div class="toolbar">
            <a href="/?view=queue">待处理 {{ page.counts.pending }}</a><a href="/?tier=3">三标签命中 {{ page.counts.three }}</a><a href="/?view=pinned">已钉住 {{ page.counts.pinned }}</a><span>近期缺席 {{ page.counts.absent }}</span>
          </div>
          <div class="queue-layout">
            <section class="queue">
              <QueueCard v-for="item in page.items" :key="item.post_id" :item="item" :pinned="page.view === 'pinned'" @action="action" />
              <p v-if="!page.items.length" class="empty">当前列表为空。</p>
            </section>
            <aside class="legend">
              <section class="panel"><h2>标签说明</h2><p><b>第一手信息</b><br>作者自身观察、调研、交易复盘或可追溯经历。</p><p><b>可迁移框架</b><br>可复用的判断方法、约束条件或推理结构。</p><p><b>有据非共识</b><br>和常见叙事有差异，并给出支撑证据或验证线索。</p></section>
              <section class="panel"><h2>操作说明</h2><p><b>钉住</b><br>把当前版本长期留观。</p><p><b>取消钉住</b><br>恢复按时间窗口观察。</p><p><b>关注理由</b><br>记录判断与预期，同时钉住版本。</p></section>
            </aside>
          </div>
        </template>

        <template v-else-if="page?.view === 'raw' || page?.view === 'filtered'">
          <div class="page-title"><div><h1>{{ page.view === "raw" ? "原始时间线" : "标签过滤流" }}</h1><p v-if="page.prompt_version" class="sub">prompt 版本 {{ page.prompt_version }}</p></div></div>
          <TimelineCard v-for="item in page.items" :key="item.post_id" :item="item" :show-labels="page.view === 'filtered'" />
          <p v-if="!page.items.length" class="empty">暂无记录。</p>
        </template>

        <template v-else-if="page?.view === 'author'">
          <div class="page-title"><AuthorBadge :item="page.profile.author" /><h1>{{ authorName(page.profile.author) }}</h1></div>
          <p class="bio">{{ page.profile.author.author_description }}</p>
          <div class="stream-label"><span class="eyebrow">最近观点簇与市场变化</span></div>
          <p v-if="page.profile.viewpoint_clusters.length && !hasMarketFeedback(page.profile.viewpoint_clusters)" class="empty soft">
            尚未导入可用行情或记录市场结果，当前先展示观点证据。
          </p>
          <ViewpointCluster v-for="cluster in page.profile.viewpoint_clusters" :key="cluster.title + cluster.latest_at" :cluster="cluster" />
          <div class="stream-label"><span class="eyebrow">最近帖子</span></div>
          <TimelineCard v-for="item in page.profile.posts" :key="item.post_id" :item="item" />
        </template>

        <template v-else-if="page?.view === 'post'">
          <div class="page-title"><div><h1>证据卡片：{{ postTitle(page.card.post) }}</h1><AuthorBadge :item="page.card.post" /></div><PostLinks :item="page.card.post" /></div>
          <section class="panel">
            <p>{{ page.card.post.status?.human_label }}</p>
            <p class="muted">{{ page.card.post.status?.deletion_signal_label }}</p>
            <div class="actions"><button @click="action(`/posts/${page.card.post.id}/pin`)">钉住</button><button class="secondary" @click="action(`/posts/${page.card.post.id}/unpin`)">取消钉住</button></div>
            <form v-if="page.card.post.current_version_id" @submit.prevent="submitAttention">
              <label>关注理由<textarea name="reason" required></textarea></label><label>我的预期<textarea name="expectation"></textarea></label><button>记录关注理由并钉住</button>
            </form>
            <button v-if="page.card.post.current_version_id" @click="action(`/posts/${page.card.post.id}/rewrite`, { version_id: page.card.post.current_version_id })">生成单条改写训练</button>
          </section>
          <section><div class="stream-label"><span class="eyebrow">观察版本</span></div><article v-for="version in page.card.versions" :key="version.version_id" class="card"><h3>观察版本 {{ version.version_id }}</h3><p class="muted">首次 {{ fmtTime(version.first_observed_at) }} · 最后 {{ fmtTime(version.last_observed_at) }}</p><pre>{{ version.content_text }}</pre><details><summary>相对上一版本 diff</summary><pre>{{ version.diff_from_prior_observed_version || "首个观察版本" }}</pre></details></article></section>
          <section v-for="name in ['feed_observations', 'direct_probes', 'events', 'attention_log', 'rewrite_exercises', 'enrichments']" :key="name"><div class="stream-label"><span class="eyebrow">{{ name }}</span></div><pre class="data">{{ JSON.stringify(page.card[name], null, 2) }}</pre></section>
        </template>
      </main>
    </div>
  </div>
</template>
