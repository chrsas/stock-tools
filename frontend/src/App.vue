<script setup lang="ts">
import { onMounted, ref } from "vue";
import { loadPage, mutate, type Row } from "./api";
import { authorName, fmtTime, percent, postTitle, xueqiuUrl } from "./format";
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
  if (!page.value || busy.value) return;
  busy.value = true;
  error.value = "";
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

function submitDecision(event: Event) {
  const form = event.currentTarget as HTMLFormElement;
  const values = Object.fromEntries(new FormData(form).entries());
  action("/decisions/add", values);
}

function submitDecisionClose(event: Event, decisionId: number) {
  const form = event.currentTarget as HTMLFormElement;
  action(`/decisions/${decisionId}/close`, Object.fromEntries(new FormData(form).entries()));
}

function submitDecisionReview(event: Event, decisionId: number) {
  const form = event.currentTarget as HTMLFormElement;
  action(`/decisions/${decisionId}/review`, Object.fromEntries(new FormData(form).entries()));
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
        <img class="logo-mark" src="/favicon.png" alt="">
        <span class="logo-text"><strong>KOL 照妖镜</strong><small class="muted">市场观点核验终端</small></span>
      </a>
      <ul class="nav">
        <li><a class="nav-item" :class="{ on: navActive('authors') }" href="/"><svg viewBox="0 0 24 24" class="ico"><path d="M3 12h4l2 6 4-14 2 8h6" /></svg>博主观点</a></li>
        <li><a class="nav-item" :class="{ on: navActive('queue') }" href="/?view=queue"><svg viewBox="0 0 24 24" class="ico"><path d="M3 12h5l2 3h4l2-3h5" /><path d="M5 5h14v14H5z" /></svg>待处理队列</a></li>
        <li><a class="nav-item" :class="{ on: navActive('pinned') }" href="/?view=pinned"><svg viewBox="0 0 24 24" class="ico"><path d="M12 17v5" /><path d="M9 3h6l-1 6 3 3v2H7v-2l3-3-1-6z" /></svg>已钉住</a></li>
        <li><a class="nav-item" :class="{ on: navActive('raw') }" href="/?view=raw"><svg viewBox="0 0 24 24" class="ico"><path d="M4 7h16" /><path d="M4 12h16" /><path d="M4 17h10" /></svg>原始时间线</a></li>
        <li><a class="nav-item" :class="{ on: navActive('filtered') }" href="/?view=filtered"><svg viewBox="0 0 24 24" class="ico"><path d="M4 5h16l-6 7v6l-4 2v-8z" /></svg>标签过滤流</a></li>
        <li><a class="nav-item" :class="{ on: navActive('claims') }" href="/?view=claims"><svg viewBox="0 0 24 24" class="ico"><path d="M5 4h14v16H5z" /><path d="M8 9h8M8 13h5" /></svg>命题确认</a></li>
        <li><a class="nav-item" :class="{ on: navActive('decisions') }" href="/?view=decisions"><svg viewBox="0 0 24 24" class="ico"><path d="M5 4h14v16H5z" /><path d="M8 8h8M8 12h8M8 16h5" /></svg>我的决策</a></li>
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

        <template v-else-if="page?.view === 'decisions'">
          <div class="page-title"><div><h1>我的决策</h1><p class="sub">记录原始论点、证伪条件、结算结果与复盘。</p></div></div>
          <div class="toolbar">
            <span>开放 {{ page.counts.open }}</span>
            <span>到期未结算 {{ page.counts.due_unresolved }}</span>
            <span>逾期未复盘 {{ page.counts.review_overdue }}</span>
          </div>
          <section class="panel">
            <h2>记录决策</h2>
            <form @submit.prevent="submitDecision">
              <label>标的代码<input name="ticker" placeholder="SH688303" required></label>
              <label>方向<select name="direction" required><option value="long">long</option><option value="short">short</option><option value="neutral">neutral</option></select></label>
              <label>观察期限（自然日）<input name="horizon_days" type="number" min="1"></label>
              <label>原始论点<textarea name="thesis" required></textarea></label>
              <label>证伪条件<textarea name="invalidation" required></textarea></label>
              <label>仓位备注<textarea name="position_note"></textarea></label>
              <label>来源帖子 ID<input name="source_post_id" type="number" min="1"></label>
              <label>来源版本 ID<input name="source_version_id" type="number" min="1"></label>
              <button :disabled="busy">记录决策</button>
            </form>
          </section>
          <form class="toolbar" method="get">
            <input type="hidden" name="view" value="decisions">
            <select name="status" :value="page.filters.status || ''"><option value="">全部状态</option><option value="open">open</option><option value="invalidated">invalidated</option><option value="expired">expired</option><option value="closed">closed</option></select>
            <input name="ticker" :value="page.filters.ticker || ''" placeholder="按标的筛选">
            <input name="from" type="date" :value="page.filters.decided_from || ''" aria-label="决策起始日期">
            <input name="to" type="date" :value="page.filters.decided_to || ''" aria-label="决策结束日期">
            <button>筛选</button>
          </form>
          <section class="stream">
            <article v-for="decision in page.items" :key="decision.id" class="card">
              <header><h2>{{ decision.ticker }}<span v-if="decision.ticker_name"> · {{ decision.ticker_name }}</span></h2><span class="pill">{{ decision.status }}</span></header>
              <p class="muted">{{ decision.direction }} · 决策时间 {{ fmtTime(decision.decided_at) }} · {{ decision.due_date ? `到期 ${decision.due_date}` : "未设期限" }}</p>
              <p v-if="decision.due_unresolved" class="error">到期未结算，等待共同交易日行情。</p>
              <p v-if="decision.review_overdue" class="error">已关闭，尚未复盘。</p>
              <h3>原始论点</h3><pre>{{ decision.thesis_text }}</pre>
              <h3>证伪条件</h3><pre>{{ decision.invalidation_condition }}</pre>
              <p v-if="decision.source_post_id"><a :href="`/posts/${decision.source_post_id}`">查看来源帖子证据</a><span v-if="decision.source_version_id" class="muted"> · 版本 {{ decision.source_version_id }}</span></p>
              <details v-if="decision.position_note || decision.notes"><summary>备注</summary><p>{{ decision.position_note }}</p><p>{{ decision.notes }}</p></details>
              <div v-if="decision.outcomes.length" class="stream-label"><span class="eyebrow">逐条结算</span></div>
              <div v-for="outcome in decision.outcomes" :key="outcome.id" class="market-row">
                <strong>{{ outcome.resolved_at }}</strong>
                <span>标的 {{ percent(outcome.raw_return) }} · {{ outcome.benchmark_ticker }} {{ percent(outcome.benchmark_return) }} · 超额 {{ percent(outcome.excess_return) }}</span>
                <small class="muted">{{ outcome.outcome_method_version }}</small>
              </div>
              <div v-if="decision.reviews.length" class="stream-label"><span class="eyebrow">复盘记录</span></div>
              <article v-for="review in decision.reviews" :key="review.id" class="statement"><p class="muted">{{ fmtTime(review.reviewed_at) }}</p><pre>{{ review.retro_text }}</pre><p v-if="review.lesson"><b>经验：</b>{{ review.lesson }}</p></article>
              <form v-if="decision.status === 'open'" @submit.prevent="submitDecisionClose($event, decision.id)">
                <label>关闭状态<select name="status" required><option value="closed">closed</option><option value="invalidated">invalidated</option><option value="expired">expired</option></select></label>
                <label>关闭备注<textarea name="notes"></textarea></label>
                <button :disabled="busy">人工关闭</button>
              </form>
              <form @submit.prevent="submitDecisionReview($event, decision.id)">
                <label>复盘<textarea name="retro" required></textarea></label>
                <label>经验<textarea name="lesson"></textarea></label>
                <button :disabled="busy">追加复盘</button>
              </form>
            </article>
            <p v-if="!page.items.length" class="empty">暂无决策记录。</p>
          </section>
        </template>

        <template v-else-if="page?.view === 'claims'">
          <div class="page-title"><div><h1>命题确认</h1><p class="sub">核对原文证据后接受或拒绝 LLM 提议。</p></div></div>
          <div class="toolbar">
            <a href="/?view=claims&state=pending">待确认 {{ page.counts.pending }}</a>
            <a href="/?view=claims&state=accepted">已接受 {{ page.counts.accepted }}</a>
            <a href="/?view=claims&state=rejected">已拒绝 {{ page.counts.rejected }}</a>
          </div>
          <section class="stream">
            <article v-for="proposal in page.items" :key="proposal.id" class="card">
              <header>
                <h2>{{ proposal.ticker }}<span v-if="proposal.ticker_name"> · {{ proposal.ticker_name }}</span></h2>
                <span class="pill">{{ proposal.review_state }}</span>
              </header>
              <p class="muted">{{ proposal.direction }} · 版本 {{ proposal.version_id }} · 首次观察 {{ fmtTime(proposal.first_observed_at) }}</p>
              <p class="muted">期限 {{ proposal.horizon_days ? `${proposal.horizon_days} 天` : "原文未说明" }} · 目标价 {{ proposal.target_price || "原文未说明" }}</p>
              <blockquote>{{ proposal.evidence_snippet }}</blockquote>
              <details><summary>查看完整原文</summary><pre>{{ proposal.content_text }}</pre></details>
              <p><a :href="`/posts/${proposal.post_id}`">查看版本证据</a></p>
              <div v-if="proposal.review_state === 'pending'" class="actions">
                <button :disabled="busy" @click="action(`/claim-proposals/${proposal.id}/review`, { review_state: 'accepted' })">接受</button>
                <button class="secondary" :disabled="busy" @click="action(`/claim-proposals/${proposal.id}/review`, { review_state: 'rejected' })">拒绝</button>
              </div>
            </article>
            <p v-if="!page.items.length" class="empty">暂无命题提议。</p>
          </section>
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
