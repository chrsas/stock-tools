<script setup lang="ts">
import { computed, ref, watch } from "vue";
import { friendlyRequestError, mutate, type Row } from "../api";
import { fmtTime, percent } from "../format";
import RecallBriefPoint from "./RecallBriefPoint.vue";

const props = defineProps<{ page: Row; busy: boolean; refresh: () => Promise<void> }>();
const emit = defineEmits<{ error: [message: string] }>();

const recallQuestion = ref("");
const recallGroups = ref<{ label: string; terms: string }[]>([]);
const recallFrom = ref("");
const recallTo = ref("");
const recallTickers = ref("");
const recallAuthors = ref<string[]>([]);
const recallWindowOpen = ref(false);
const recallAnyGroup = ref(false);
const recallLimit = ref(200);
const recallExpanding = ref(false);
const recallNotes = ref("");
const recallNotice = ref("");
const recallBriefGenerating = ref(false);
const recallBrief = ref<Row | null>(null);
const recallBriefNotice = ref("");

function syncRecallForm() {
  const form = (props.page.form || {}) as Row;
  recallQuestion.value = String(form.question || "");
  recallGroups.value = mapRecallGroups(form.groups);
  recallFrom.value = String(form.date_from || "");
  recallTo.value = String(form.date_to || "");
  recallTickers.value = (Array.isArray(form.tickers) ? form.tickers : []).join(", ");
  recallAuthors.value = (Array.isArray(form.authors) ? form.authors : []).map(String);
  recallAnyGroup.value = !form.require_all_groups;
  recallLimit.value = Number(form.limit || 200);
  // Fresh page (no window chosen yet) lands on the last half month, so a search can
  // run without expanding the collapsed time-window box. A window from the URL/echo
  // is kept as-is. The box stays collapsed unless an error needs the dates shown.
  if (!recallFrom.value && !recallTo.value) applyRecallPreset("halfMonth");
  recallWindowOpen.value = Boolean(props.page.error);
}

watch(() => props.page, syncRecallForm, { immediate: true });

function onRecallWindowToggle(event: Event) {
  recallWindowOpen.value = (event.target as HTMLDetailsElement).open;
}

function mapRecallGroups(groups: unknown): { label: string; terms: string }[] {
  if (!Array.isArray(groups)) return [];
  return groups.map((group: Row) => ({
    label: String(group.label || ""),
    terms: (Array.isArray(group.terms) ? group.terms : []).join(", "),
  }));
}

function addRecallGroup() {
  recallGroups.value.push({ label: "", terms: "" });
}

function removeRecallGroup(index: number) {
  recallGroups.value.splice(index, 1);
}

function splitRecallTerms(value: string): string[] {
  return value.split(/[,，、]/).map((item) => item.trim()).filter(Boolean);
}

async function expandRecall() {
  if (recallExpanding.value || props.busy) return;
  const question = recallQuestion.value.trim();
  if (!question) { emit("error", "请先输入主题问题。"); return; }
  recallExpanding.value = true;
  emit("error", "");
  recallNotice.value = "";
  try {
    const result = await mutate("/recall/expand", props.page.csrf_token, { question });
    const groups = mapRecallGroups(result.groups);
    if (groups.length) recallGroups.value = groups;
    if (result.date_from) recallFrom.value = String(result.date_from);
    if (result.date_to) recallTo.value = String(result.date_to);
    const tickers = Array.isArray(result.tickers) ? result.tickers : [];
    if (tickers.length) recallTickers.value = tickers.join(", ");
    recallNotes.value = String(result.notes || "");
    recallNotice.value = "已生成建议检索词，请确认或修改后再检索（确定性检索不会再调用模型）。";
  } catch (reason) {
    emit("error", friendlyRequestError(reason));
  } finally {
    recallExpanding.value = false;
  }
}

function recallParams(): URLSearchParams {
  const params = new URLSearchParams();
  params.set("view", "recall");
  const question = recallQuestion.value.trim();
  if (question) params.set("q", question);
  for (const group of recallGroups.value) {
    const label = group.label.trim();
    const terms = splitRecallTerms(group.terms);
    if (label && terms.length) params.append("group", `${label}=${terms.join(",")}`);
  }
  if (recallFrom.value) params.set("from", recallFrom.value);
  if (recallTo.value) params.set("to", recallTo.value);
  for (const ticker of splitRecallTerms(recallTickers.value)) params.append("ticker", ticker);
  for (const uid of recallAuthors.value) if (uid) params.append("author", uid);
  if (recallAnyGroup.value) params.set("any", "1");
  if (recallLimit.value) params.set("limit", String(recallLimit.value));
  return params;
}

function submitRecall() {
  // Retrieval is bounded by the start date; the end date is optional (a lone start
  // means that single day) and narrowing (groups / authors / tickers) is optional too
  // — a window alone returns "那段时间博主们说了啥". Validate the start date client-side
  // so a missing date gives instant feedback instead of a silent no-op.
  const params = recallParams();
  if (!recallFrom.value) {
    emit("error", "请填写回溯时间窗的起始日期（北京时间）。");
    return;
  }
  emit("error", "");
  window.location.assign(`/?${params.toString()}`);
}

function fmtRecallDate(d: Date): string {
  const y = d.getFullYear();
  const m = String(d.getMonth() + 1).padStart(2, "0");
  const day = String(d.getDate()).padStart(2, "0");
  return `${y}-${m}-${day}`;
}

// Quick time-window presets. Calendar presets (本周/上周/本月/上月) use real period
// bounds; this-week/this-month cap the end at today since the future has no data.
// Relative presets count back from today (Beijing local clock on the user's machine).
function applyRecallPreset(kind: string) {
  const today = new Date();
  today.setHours(0, 0, 0, 0);
  let from = new Date(today);
  let to = new Date(today);
  const mondayOffset = (today.getDay() + 6) % 7; // Mon=0 … Sun=6
  if (kind === "day") {
    // 最近一天：只看今天。
  } else if (kind === "last7") {
    from.setDate(today.getDate() - 6);
  } else if (kind === "halfMonth") {
    from.setDate(today.getDate() - 14);
  } else if (kind === "thisWeek") {
    from.setDate(today.getDate() - mondayOffset);
  } else if (kind === "lastWeek") {
    from.setDate(today.getDate() - mondayOffset - 7);
    to = new Date(from);
    to.setDate(from.getDate() + 6);
  } else if (kind === "thisMonth") {
    from = new Date(today.getFullYear(), today.getMonth(), 1);
  } else if (kind === "lastMonth") {
    from = new Date(today.getFullYear(), today.getMonth() - 1, 1);
    to = new Date(today.getFullYear(), today.getMonth(), 0);
  } else if (kind === "last30") {
    from.setDate(today.getDate() - 29);
  } else if (kind === "lastQuarter") {
    from.setMonth(today.getMonth() - 3);
  }
  recallFrom.value = fmtRecallDate(from);
  recallTo.value = fmtRecallDate(to);
}

const RECALL_PRESETS: { kind: string; label: string }[] = [
  { kind: "day", label: "最近一天" },
  { kind: "last7", label: "最近一周" },
  { kind: "halfMonth", label: "最近半个月" },
  { kind: "thisWeek", label: "本周" },
  { kind: "lastWeek", label: "上周" },
  { kind: "thisMonth", label: "本月" },
  { kind: "lastMonth", label: "上月" },
  { kind: "last30", label: "最近一个月" },
  { kind: "lastQuarter", label: "最近一个季度" },
];

function confirmedRecallParams(): URLSearchParams {
  // The brief must be synthesized over the *confirmed* query that produced the
  // displayed results (echoed back as page.form), never the live editable form —
  // otherwise an unsubmitted edit would archive a brief against conditions the user
  // isn't looking at, and spend a model call doing it. Rebuilt to byte-match the GET
  // params submitRecall would send for the same confirmed query.
  const form = (props.page.form || {}) as Row;
  const params = new URLSearchParams();
  params.set("view", "recall");
  const question = String(form.question || "").trim();
  if (question) params.set("q", question);
  for (const group of (Array.isArray(form.groups) ? form.groups : []) as Row[]) {
    const label = String(group.label || "").trim();
    const terms = (Array.isArray(group.terms) ? group.terms : [])
      .map((term: unknown) => String(term).trim())
      .filter(Boolean);
    if (label && terms.length) params.append("group", `${label}=${terms.join(",")}`);
  }
  if (form.date_from) params.set("from", String(form.date_from));
  if (form.date_to) params.set("to", String(form.date_to));
  for (const ticker of (Array.isArray(form.tickers) ? form.tickers : []) as unknown[]) {
    const value = String(ticker).trim();
    if (value) params.append("ticker", value);
  }
  for (const uid of (Array.isArray(form.authors) ? form.authors : []) as unknown[]) {
    const value = String(uid).trim();
    if (value) params.append("author", value);
  }
  if (!form.require_all_groups) params.set("any", "1");
  if (form.limit) params.set("limit", String(form.limit));
  return params;
}

function recallFormDiverged(): boolean {
  // True when the editable form no longer matches the confirmed query behind the
  // shown results — the user changed terms/window without re-searching.
  return recallParams().toString() !== confirmedRecallParams().toString();
}

// Coverage points that honestly flag a thin / concentrated sample are the governance
// signal the reader should see first, so we lift them out of the bullet list into a
// callout (see recallSplitCoverage). Keyword set tracks the phrasing the brief system
// prompt mandates ("样本少，不足以代表共识") plus common honesty hedges.
const RECALL_WARNING_RE =
  /样本(少|偏少|不足|有限)|不足以代表|不能代表|代表性不足|来源(集中|单一|有限)|集中(于|在)|高估|偏“?干净”?|谨慎(解读|对待)|盲区|幸存/;

function recallIsWarning(point: Row): boolean {
  return RECALL_WARNING_RE.test(String(point.text || ""));
}

function recallSplitCoverage(points: Row[]): { warnings: Row[]; rest: Row[] } {
  const warnings: Row[] = [];
  const rest: Row[] = [];
  for (const point of points) (recallIsWarning(point) ? warnings : rest).push(point);
  return { warnings, rest };
}

function recallJudgementGroups(points: Row[]): { key: string; author: string; points: Row[] }[] {
  // Group the 当时判断 block per author so each person's timeline reads as one continuous
  // thread (看多→减仓→抄底) instead of points scattered across authors. A point that rests
  // on exactly one author joins that author's group; anything spanning authors (or none)
  // falls into a shared 多位作者 group. Within a group, points sort by date ascending.
  const groups = new Map<string, { author: string; points: Row[] }>();
  const order: string[] = [];
  for (const point of points) {
    const authors = Array.isArray(point.authors) ? (point.authors as string[]) : [];
    const single = authors.length === 1 && Boolean(authors[0]);
    const key = single ? `a:${authors[0]}` : "multi";
    const author = single ? String(authors[0]) : "多位作者";
    if (!groups.has(key)) {
      groups.set(key, { author, points: [] });
      order.push(key);
    }
    groups.get(key)!.points.push(point);
  }
  for (const group of groups.values()) {
    group.points.sort((a, b) =>
      String(a.date_label || "").localeCompare(String(b.date_label || "")),
    );
  }
  return order.map((key) => ({ key, ...groups.get(key)! }));
}

// History briefs persist only brief_text — the deterministic markdown render of the
// sections (_render_brief_text). Reconstruct the same block structure from it so an
// archived brief reads with the same clarity as a freshly generated one (section heads,
// lifted sample warnings, collapsible citation chains) instead of a raw <pre> dump.
// Authors are intentionally not folded into brief_text, so the 当时判断 block reads here as
// a flat timeline (no per-author grouping) and citations show v-ids without post links —
// the live panel still owns the fully-linked, author-grouped view.
const RECALL_BLOCK_KEYS: Record<string, string> = {
  覆盖度: "coverage",
  当时判断: "contemporaneous_judgement",
  后来描述性结果: "later_descriptive_outcome",
  缺口与反证: "gaps_and_counterevidence",
};

function parseBriefPoint(body: string): Row {
  const cite = body.match(/〔([^〕]*)〕\s*$/);
  if (!cite) return { text: body, date_label: "", version_ids: [] } as unknown as Row;
  const inner = cite[1];
  const versionIds = [...inner.matchAll(/v(\d+)/g)].map((match) => Number(match[1]));
  const sep = inner.indexOf(" · ");
  const dateLabel = sep >= 0 ? inner.slice(0, sep).trim() : "";
  return {
    text: body.slice(0, cite.index).trim(),
    date_label: dateLabel,
    version_ids: versionIds,
  } as unknown as Row;
}

function parseBriefSections(briefText: string): { key: string; title: string; points: Row[] }[] {
  const sections: { key: string; title: string; points: Row[] }[] = [];
  let current: { key: string; title: string; points: Row[] } | null = null;
  for (const raw of String(briefText || "").split("\n")) {
    const line = raw.trim();
    if (line.startsWith("## ")) {
      const title = line.slice(3).trim();
      current = { key: RECALL_BLOCK_KEYS[title] || title, title, points: [] };
      sections.push(current);
    } else if (current && line.startsWith("- ")) {
      const body = line.slice(2).trim();
      if (body !== "（本次未生成该部分内容）") current.points.push(parseBriefPoint(body));
    }
  }
  return sections.filter((section) => section.points.length);
}

const historyBriefViews = computed<Row[]>(() =>
  (((props.page.briefs as Row[]) ?? []) as Row[]).map((brief): Row => ({
    ...brief,
    sections: parseBriefSections(String(brief.brief_text || "")),
  })),
);

async function generateRecallBrief() {
  if (recallBriefGenerating.value || props.busy) return;
  const params = confirmedRecallParams();
  if (!params.get("q")) { emit("error", "请先填写主题问题并检索，简报需要可追溯的标题。"); return; }
  recallBriefGenerating.value = true;
  emit("error", "");
  recallBriefNotice.value = "";
  try {
    const result = await mutate("/recall/brief", props.page.csrf_token, params);
    recallBrief.value = result;
    recallBriefNotice.value = "已生成简报并归档（append-only，不可改写）。";
    await props.refresh();
  } catch (reason) {
    emit("error", friendlyRequestError(reason));
  } finally {
    recallBriefGenerating.value = false;
  }
}
</script>

<template>
  <div class="page-title"><div><h1>主题回溯</h1><p class="sub">按事件 + 时间窗回溯当时发言。确定性检索：纯证据、零幻觉、不调用模型，可逐条核对。</p></div></div>
  <section class="panel">
    <label>主题问题<input v-model="recallQuestion" placeholder="如：美伊冲突那阵子大家怎么看油价" @keyup.enter="submitRecall"></label>
    <div class="actions">
      <button class="secondary" :disabled="recallExpanding || busy" @click="expandRecall">{{ recallExpanding ? "扩词中…" : "扩词建议" }}</button>
      <span class="muted small">扩词会调用模型，仅给出可改的检索词与建议时间窗，不生成结论、不下判断。</span>
    </div>
    <p v-if="recallNotice" class="notice">{{ recallNotice }}</p>
    <p v-if="recallNotes" class="muted small">模型说明：{{ recallNotes }}</p>
    <div class="stream-label"><span class="eyebrow">检索词分组（可选 · 组内 OR、组间默认 AND）</span></div>
    <p class="muted small">不填分组时，按下方时间窗（可叠加博主/标的）回看当时所有发言。</p>
    <div v-for="(group, index) in recallGroups" :key="index" class="recall-group">
      <input v-model="group.label" placeholder="维度名 如 event">
      <input v-model="group.terms" placeholder="同义词，逗号分隔 如 美伊,伊朗,霍尔木兹">
      <button class="secondary" @click="removeRecallGroup(index)">删除</button>
    </div>
    <div class="actions"><button class="secondary" @click="addRecallGroup">+ 添加分组</button></div>
    <details class="recall-window-box" :open="recallWindowOpen" @toggle="onRecallWindowToggle">
      <summary><span class="eyebrow">时间窗</span> <span class="muted small">{{ recallFrom || "未设起始" }} ~ {{ recallTo || "起始当天" }}</span></summary>
      <p class="muted small">只填起始即可，结束为空默认起始当天；默认最近半个月。</p>
      <div class="recall-presets">
        <button v-for="preset in RECALL_PRESETS" :key="preset.kind" type="button" class="secondary" @click="applyRecallPreset(preset.kind)">{{ preset.label }}</button>
      </div>
      <div class="recall-window">
        <label>起始日期<input v-model="recallFrom" type="date"></label>
        <label>结束日期（可选，默认起始当天）<input v-model="recallTo" type="date"></label>
      </div>
    </details>
    <div class="recall-window">
      <label>标的过滤（可选，逗号分隔）<input v-model="recallTickers" placeholder="SH601857"></label>
      <label>最多命中<input v-model.number="recallLimit" type="number" min="1" max="500"></label>
    </div>
    <label v-if="page.author_options?.length" class="recall-authors">
      <span>博主过滤（可选，多选；按住 Ctrl/⌘ 选多位）</span>
      <select v-model="recallAuthors" multiple size="5">
        <option v-for="opt in page.author_options" :key="opt.uid" :value="opt.uid">{{ opt.name }}（{{ opt.version_count }}）</option>
      </select>
      <button v-if="recallAuthors.length" type="button" class="secondary" @click="recallAuthors = []">清空所选博主</button>
    </label>
    <label class="toggle-row"><input v-model="recallAnyGroup" type="checkbox"><span>组间改为 OR（放宽召回；默认 AND 提高精度）</span></label>
    <div class="actions"><button :disabled="busy" @click="submitRecall">检索</button></div>
  </section>

  <p v-if="page.form?.invalid_groups?.length" class="error">无法解析的分组：{{ page.form.invalid_groups.join(" · ") }}（应写成 label=词1,词2）。</p>
  <p v-if="page.error" class="error">{{ page.error }}</p>

  <template v-if="page.has_results">
    <div class="toolbar">
      <span>命中版本 {{ page.coverage.version_count }}</span>
      <span>博主 {{ page.coverage.author_count }}</span>
      <span>帖子 {{ page.coverage.post_count }}</span>
      <span>组间 {{ page.coverage.require_all_groups ? "AND" : "OR" }}</span>
      <span v-if="page.selection.removed_post_count">来源页曾明确已移除 {{ page.selection.removed_post_count }} 帖</span>
    </div>
    <div v-if="page.coverage.groups?.length" class="toolbar">
      <span class="muted small">各组窗内命中：</span>
      <span v-for="g in page.coverage.groups" :key="g.label">{{ g.label }} {{ g.version_count }}</span>
    </div>
    <p v-if="page.selection.removed_post_count" class="muted small">检索只覆盖现存归档，删帖会让画面偏“干净”；上方中性列出曾被移除的帖子数，便于折扣解读，不做归因。</p>
    <div class="actions">
      <button :disabled="recallBriefGenerating || busy" @click="generateRecallBrief">{{ recallBriefGenerating ? "合成简报中…" : "在当前结果上生成简报" }}</button>
      <span class="muted small">简报会调用模型，按当前显示结果的已确认条件合成，固定四块、每条带 version_id 引用，并连同当时的覆盖度/选择性一起归档（不可改写）。</span>
    </div>
    <p v-if="recallFormDiverged()" class="muted small">上方检索条件已修改但尚未重新检索；简报仍按当前显示的结果生成。如需对修改后的条件生成，请先点「检索」。</p>
    <p v-if="recallBriefNotice" class="notice">{{ recallBriefNotice }}</p>
    <section v-if="recallBrief" class="panel recall-brief">
      <div class="stream-label"><span class="eyebrow">本次简报 · {{ recallBrief.prompt_version }} · 引用 {{ (recallBrief.cited_version_ids || []).length }} 个版本</span></div>
      <div v-for="section in recallBrief.sections" :key="section.key" class="recall-brief-block" :data-block="section.key">
        <div class="recall-brief-head"><span class="eyebrow">{{ section.title }}</span></div>
        <p v-if="!section.points.length" class="muted small">（本次未生成该部分内容）</p>

        <!-- 覆盖度：把样本少/来源集中这类诚实提示提为醒目 note（仍保留引用链），普通统计走列表 -->
        <template v-if="section.key === 'coverage'">
          <RecallBriefPoint v-for="(point, index) in recallSplitCoverage(section.points).warnings" :key="`w${index}`" :point="point" :hits="page.hits" variant="warn" />
          <ul v-if="recallSplitCoverage(section.points).rest.length">
            <RecallBriefPoint v-for="(point, index) in recallSplitCoverage(section.points).rest" :key="index" :point="point" :hits="page.hits" />
          </ul>
        </template>

        <!-- 当时判断：按作者分组，让每个人的时间线连续可读 -->
        <template v-else-if="section.key === 'contemporaneous_judgement'">
          <div v-for="group in recallJudgementGroups(section.points)" :key="group.key" class="brief-author-group">
            <h4 class="brief-author">{{ group.author }}</h4>
            <ul>
              <RecallBriefPoint v-for="(point, index) in group.points" :key="index" :point="point" :hits="page.hits" />
            </ul>
          </div>
        </template>

        <ul v-else>
          <RecallBriefPoint v-for="(point, index) in section.points" :key="index" :point="point" :hits="page.hits" />
        </ul>
      </div>
    </section>
    <section class="stream">
      <article v-for="hit in page.hits" :key="hit.version_id" class="card">
        <header>
          <h2>{{ hit.author_display_name || hit.author_platform_uid }}</h2>
          <span class="pill">{{ hit.removed ? "来源页曾明确已移除" : hit.source_state }}</span>
        </header>
        <p class="muted">发言时间 {{ fmtTime(hit.viewpoint_at) }} · 版本 {{ hit.version_id }}</p>
        <p v-if="hit.stance_summary"><b>立场摘要：</b>{{ hit.stance_summary }}</p>
        <pre>{{ hit.content_text }}</pre>
        <p v-if="hit.framework_topics?.length"><b>框架主题：</b><span v-for="topic in hit.framework_topics" :key="topic" class="pill">{{ topic }}</span></p>
        <div v-if="hit.market_snapshot" class="market-row"><strong>描述性市场变化</strong><span>标的 {{ percent(hit.market_snapshot.raw_return) }} · 超额 {{ percent(hit.market_snapshot.excess_return) }}</span></div>
        <p><a :href="`/posts/${hit.post_id}`">查看版本证据</a><a v-if="hit.url" :href="hit.url" target="_blank" rel="noopener noreferrer" class="xq-jump"> · 原帖 ↗</a></p>
      </article>
      <p v-if="!page.hits.length" class="empty">该时间窗内未检索到同时命中各分组的发言。可放宽时间窗、增删检索词，或勾选「组间改为 OR」。</p>
    </section>
  </template>

  <template v-if="historyBriefViews.length">
    <div class="stream-label"><span class="eyebrow">历史简报 {{ historyBriefViews.length }}</span></div>
    <section class="stream">
      <article v-for="brief in historyBriefViews" :key="brief.id" class="card recall-brief">
        <header>
          <h2>{{ brief.question }}</h2>
          <span class="pill">{{ brief.prompt_version }}</span>
        </header>
        <p class="muted">生成 {{ fmtTime(brief.created_at) }} · 窗 {{ fmtTime(brief.date_from) }} 至 {{ fmtTime(brief.date_to) }} · 组间 {{ brief.require_all_groups ? "AND" : "OR" }} · 引用 {{ brief.cited_count }} 个版本</p>
        <p class="muted small">
          <span v-for="g in brief.groups" :key="g.label" class="pill">{{ g.label }}：{{ (g.terms || []).join("/") }}</span>
          <span v-if="brief.coverage">· 命中版本 {{ brief.coverage.version_count }} · 博主 {{ brief.coverage.author_count }} · 帖子 {{ brief.coverage.post_count }}</span>
          <span v-if="brief.selection?.removed_post_count">· 曾被移除 {{ brief.selection.removed_post_count }} 帖</span>
        </p>
        <template v-if="brief.sections.length">
          <div v-for="section in brief.sections" :key="section.key" class="recall-brief-block" :data-block="section.key">
            <div class="recall-brief-head"><span class="eyebrow">{{ section.title }}</span></div>
            <template v-if="section.key === 'coverage'">
              <RecallBriefPoint v-for="(point, index) in recallSplitCoverage(section.points).warnings" :key="`w${index}`" :point="point" variant="warn" />
              <ul v-if="recallSplitCoverage(section.points).rest.length">
                <RecallBriefPoint v-for="(point, index) in recallSplitCoverage(section.points).rest" :key="index" :point="point" />
              </ul>
            </template>
            <ul v-else>
              <RecallBriefPoint v-for="(point, index) in section.points" :key="index" :point="point" />
            </ul>
          </div>
        </template>
        <pre v-else>{{ brief.brief_text }}</pre>
      </article>
    </section>
  </template>
</template>
