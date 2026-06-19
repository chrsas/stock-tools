<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from "vue";
import {
  friendlyRequestError,
  loadAutomationSettings,
  loadCollectionStatus,
  loadEnrichmentStatus,
  loadOperationsStatus,
  loadPage,
  mutate,
  type Row,
} from "./api";
import { authorName, fmtTime, percent, postTitle, xueqiuUrl } from "./format";
import AuthorBadge from "./components/AuthorBadge.vue";
import PostLinks from "./components/PostLinks.vue";
import QueueCard from "./components/QueueCard.vue";
import RecallBriefPoint from "./components/RecallBriefPoint.vue";
import TimelineCard from "./components/TimelineCard.vue";
import ViewpointCluster from "./components/ViewpointCluster.vue";

const page = ref<Row | null>(null);
const error = ref("");
const busy = ref(false);
const collecting = ref(false);
const collectNotice = ref("");
const collectPhase = ref("");
const collectElapsed = ref(0);
const collectLogs = ref<Row[]>([]);
const enriching = ref(false);
const enrichPhase = ref("");
const enrichProcessed = ref(0);
const enrichTotal = ref(0);
const enrichNotice = ref("");
const enrichDetails = ref<Row[]>([]);
const enrichLogs = ref<Row[]>([]);
const automationSettings = ref<Row>({
  collection_enabled: false,
  collection_interval_minutes: 180,
  auto_enrich: true,
  next_collection_at: null,
});
const persistedAutomationSettings = ref<Row | null>(null);
const automationNotice = ref("");
const automationSaving = ref(false);
const addAuthorNotice = ref("");
const theme = ref(localStorage.getItem("kol-theme") || "system");
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
let collectStatusTimer: number | undefined;
let pollingCollectStatus = false;
let enrichStatusTimer: number | undefined;
let pollingEnrichStatus = false;
let operationsStatusTimer: number | undefined;
let operationsPollingActive = false;
let pollingOperationsStatus = false;
let activeEnrichAuthorUid = "";
let enrichRunObserved = false;
let enrichRequestPending = false;
const OPERATIONS_ACTIVE_POLL_MS = 3000;
const OPERATIONS_IDLE_POLL_MS = 30000;
const OPERATIONS_HIDDEN_POLL_MS = 120000;

function applyTheme() {
  document.documentElement.dataset.theme = theme.value === "system"
    ? matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light"
    : theme.value;
  localStorage.setItem("kol-theme", theme.value);
}

async function refresh() {
  busy.value = true;
  error.value = "";
  try { page.value = await loadPage(); syncRecallForm(); }
  catch (reason) { error.value = String(reason); }
  finally { busy.value = false; }
}

function syncRecallForm() {
  if (page.value?.view !== "recall") return;
  const form = (page.value.form || {}) as Row;
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
  recallWindowOpen.value = Boolean(page.value?.error);
}

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
  if (!page.value || recallExpanding.value || busy.value) return;
  const question = recallQuestion.value.trim();
  if (!question) { error.value = "请先输入主题问题。"; return; }
  recallExpanding.value = true;
  error.value = "";
  recallNotice.value = "";
  try {
    const result = await mutate("/recall/expand", page.value.csrf_token, { question });
    const groups = mapRecallGroups(result.groups);
    if (groups.length) recallGroups.value = groups;
    if (result.date_from) recallFrom.value = String(result.date_from);
    if (result.date_to) recallTo.value = String(result.date_to);
    const tickers = Array.isArray(result.tickers) ? result.tickers : [];
    if (tickers.length) recallTickers.value = tickers.join(", ");
    recallNotes.value = String(result.notes || "");
    recallNotice.value = "已生成建议检索词，请确认或修改后再检索（确定性检索不会再调用模型）。";
  } catch (reason) {
    error.value = friendlyRequestError(reason);
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
    error.value = "请填写回溯时间窗的起始日期（北京时间）。";
    return;
  }
  error.value = "";
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
  const form = (page.value?.form || {}) as Row;
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
  (((page.value?.briefs as Row[]) ?? []) as Row[]).map((brief): Row => ({
    ...brief,
    sections: parseBriefSections(String(brief.brief_text || "")),
  })),
);

async function generateRecallBrief() {
  if (!page.value || recallBriefGenerating.value || busy.value) return;
  const params = confirmedRecallParams();
  if (!params.get("q")) { error.value = "请先填写主题问题并检索，简报需要可追溯的标题。"; return; }
  recallBriefGenerating.value = true;
  error.value = "";
  recallBriefNotice.value = "";
  try {
    const result = await mutate("/recall/brief", page.value.csrf_token, params);
    recallBrief.value = result;
    recallBriefNotice.value = "已生成简报并归档（append-only，不可改写）。";
    await refresh();
  } catch (reason) {
    error.value = friendlyRequestError(reason);
  } finally {
    recallBriefGenerating.value = false;
  }
}

async function runCollection() {
  if (!page.value || collecting.value || enriching.value || busy.value) return;
  collecting.value = true;
  error.value = "";
  collectNotice.value = "";
  collectPhase.value = "正在启动采集";
  collectElapsed.value = 0;
  startCollectStatusPolling();
  try {
    const result = await mutate("/collect/run-once", page.value.csrf_token);
    const resultMessage = String(result.message || "采集完成。");
    await refresh();
    await restoreAutomationSettings();
    collectNotice.value = resultMessage;
    window.setTimeout(() => { void restoreEnrichStatus(); }, 300);
  } catch (reason) {
    error.value = friendlyRequestError(reason);
  } finally {
    collecting.value = false;
    await pollCollectStatus();
    stopCollectStatusPolling();
  }
}

async function pollCollectStatus() {
  if (pollingCollectStatus) return;
  pollingCollectStatus = true;
  try {
    const status = await loadCollectionStatus();
    applyCollectionStatus(status);
  } catch {
    // The main collection request reports connection failures with actionable text.
  } finally {
    pollingCollectStatus = false;
  }
}

function applyCollectionStatus(status: Row) {
  collectPhase.value = String(status.phase || "正在采集");
  collectElapsed.value = Number(status.elapsed_seconds || 0);
  collectLogs.value = Array.isArray(status.logs) ? status.logs : [];
  collecting.value = Boolean(status.running);
}

function startCollectStatusPolling() {
  stopCollectStatusPolling();
  void pollCollectStatus();
  collectStatusTimer = window.setInterval(() => { void pollCollectStatus(); }, 1000);
}

function stopCollectStatusPolling() {
  if (collectStatusTimer !== undefined) {
    window.clearInterval(collectStatusTimer);
    collectStatusTimer = undefined;
  }
}

async function runEnrichment(author: Row) {
  if (!page.value || enriching.value || enrichRequestPending || collecting.value || busy.value) return;
  activeEnrichAuthorUid = String(author.author_platform_uid);
  enrichRunObserved = false;
  enrichRequestPending = true;
  enriching.value = true;
  error.value = "";
  enrichNotice.value = "";
  enrichDetails.value = [];
  enrichLogs.value = [];
  enrichPhase.value = "正在准备富化";
  enrichProcessed.value = 0;
  enrichTotal.value = Number(author.pending_enrichment_count || 0);
  startEnrichStatusPolling();
  try {
    const uid = encodeURIComponent(activeEnrichAuthorUid);
    const result = await mutate(`/authors/${uid}/enrich`, page.value.csrf_token);
    await refresh();
    enrichNotice.value = String(result.message || "富化完成。");
    enrichDetails.value = Array.isArray(result.details) ? result.details : [];
  } catch (reason) {
    error.value = friendlyRequestError(reason);
  } finally {
    enrichRequestPending = false;
    enriching.value = false;
    await pollEnrichStatus();
    stopEnrichStatusPolling();
  }
}

async function pollEnrichStatus() {
  if (pollingEnrichStatus) return;
  pollingEnrichStatus = true;
  try {
    const status = await loadEnrichmentStatus();
    applyEnrichmentStatus(status);
  } catch {
    // The main enrichment request reports connection failures with actionable text.
  } finally {
    pollingEnrichStatus = false;
  }
}

function applyEnrichmentStatus(status: Row, acceptAnyAuthor = false) {
  const authorUid = String(status.author_uid || "");
  if (!acceptAnyAuthor && authorUid !== activeEnrichAuthorUid && !status.running) return;
  if (!acceptAnyAuthor && !enrichRunObserved && !status.running && enrichRequestPending) return;
  activeEnrichAuthorUid = authorUid;
  if (status.running) enrichRunObserved = true;
  enrichPhase.value = String(status.phase || "正在富化");
  enrichProcessed.value = Number(status.processed || 0);
  enrichTotal.value = Number(status.total || 0);
  enrichDetails.value = Array.isArray(status.details) ? status.details : [];
  enrichLogs.value = Array.isArray(status.logs) ? status.logs : [];
  enriching.value = Boolean(status.running);
  if (!status.running && enrichProcessed.value > 0) {
    enrichNotice.value = `${enrichPhase.value}。`;
  }
  if (!status.running) stopEnrichStatusPolling();
}

function startEnrichStatusPolling() {
  stopEnrichStatusPolling();
  void pollEnrichStatus();
  enrichStatusTimer = window.setInterval(() => { void pollEnrichStatus(); }, 1000);
}

function stopEnrichStatusPolling() {
  if (enrichStatusTimer !== undefined) {
    window.clearInterval(enrichStatusTimer);
    enrichStatusTimer = undefined;
  }
}

async function restoreEnrichStatus() {
  try {
    const status = await loadEnrichmentStatus();
    applyEnrichmentStatus(status, true);
    if (status.running) startEnrichStatusPolling();
  } catch {
    // A status lookup failure must not block the rest of the page on mount.
  }
}

async function restoreCollectStatus() {
  try {
    const status = await loadCollectionStatus();
    applyCollectionStatus(status);
    if (status.running) startCollectStatusPolling();
  } catch {
    // A status lookup failure must not block the rest of the page on mount.
  }
}

async function restoreAutomationSettings() {
  try {
    const settings = await loadAutomationSettings();
    automationSettings.value = settings;
    persistedAutomationSettings.value = settings;
  } catch {
    // Automation settings do not block archive browsing.
  }
}

function automationIntervalMinutes(): number | null {
  const value = automationSettings.value.collection_interval_minutes;
  if (value === "" || value == null) return null;
  const minutes = Number(value);
  return Number.isInteger(minutes) && minutes >= 5 && minutes <= 10080 ? minutes : null;
}

async function saveAutomationSettings() {
  if (!page.value || busy.value || automationSaving.value) return;
  const interval = automationIntervalMinutes();
  if (interval === null) {
    automationNotice.value = "请先填写 5 至 10080 之间的采集周期。";
    return;
  }
  const wasEnabled = Boolean(persistedAutomationSettings.value?.collection_enabled);
  automationSaving.value = true;
  busy.value = true;
  error.value = "";
  automationNotice.value = "";
  try {
    const settings = await mutate("/automation/settings", page.value.csrf_token, {
      collection_enabled: String(Boolean(automationSettings.value.collection_enabled)),
      collection_interval_minutes: String(interval),
      auto_enrich: String(Boolean(automationSettings.value.auto_enrich)),
    });
    automationSettings.value = settings;
    persistedAutomationSettings.value = settings;
    automationNotice.value = settings.collection_enabled && !wasEnabled
      ? "自动化配置已保存，首轮采集会立即启动。"
      : "自动化配置已保存。";
  } catch (reason) {
    error.value = friendlyRequestError(reason);
  } finally {
    automationSaving.value = false;
    busy.value = false;
  }
}

function enrichmentProgress(): number {
  if (!enrichTotal.value) return 0;
  return Math.min(100, Math.round((enrichProcessed.value / enrichTotal.value) * 100));
}

function operationsPollDelay(): number {
  if (document.hidden) return OPERATIONS_HIDDEN_POLL_MS;
  return collecting.value || enriching.value ? OPERATIONS_ACTIVE_POLL_MS : OPERATIONS_IDLE_POLL_MS;
}

function scheduleOperationsStatusPolling(delay = operationsPollDelay()) {
  if (!operationsPollingActive) return;
  clearOperationsStatusTimer();
  operationsStatusTimer = window.setTimeout(() => { void pollOperationsStatus(); }, delay);
}

async function pollOperationsStatus() {
  if (!operationsPollingActive || pollingOperationsStatus || page.value?.view !== "operations") return;
  pollingOperationsStatus = true;
  try {
    if (collectStatusTimer !== undefined || enrichStatusTimer !== undefined) return;
    const status = await loadOperationsStatus();
    applyCollectionStatus(status.collection || {});
    applyEnrichmentStatus(status.enrichment || {}, true);
    if (!automationSaving.value) {
      automationSettings.value = status.automation || automationSettings.value;
      persistedAutomationSettings.value = status.automation || persistedAutomationSettings.value;
    }
  } catch {
    // The dedicated actions report connection failures with actionable text.
  } finally {
    pollingOperationsStatus = false;
    if (operationsPollingActive && page.value?.view === "operations") scheduleOperationsStatusPolling();
  }
}

function startOperationsStatusPolling() {
  if (operationsPollingActive) return;
  operationsPollingActive = true;
  void pollOperationsStatus();
}

function clearOperationsStatusTimer() {
  if (operationsStatusTimer !== undefined) {
    window.clearTimeout(operationsStatusTimer);
    operationsStatusTimer = undefined;
  }
}

function stopOperationsStatusPolling() {
  operationsPollingActive = false;
  clearOperationsStatusTimer();
}

function handleVisibilityChange() {
  if (operationsPollingActive && page.value?.view === "operations") {
    scheduleOperationsStatusPolling(operationsPollDelay());
  }
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

async function submitAddAuthor(event: Event) {
  if (!page.value || busy.value) return;
  const form = event.currentTarget as HTMLFormElement;
  const values = Object.fromEntries(new FormData(form).entries()) as Row;
  busy.value = true;
  error.value = "";
  addAuthorNotice.value = "";
  try {
    const result = await mutate("/accounts/add", page.value.csrf_token, values);
    addAuthorNotice.value = result.status === "exists"
      ? `博主 ${result.uid} 已在追踪列表中。`
      : `已登记博主 ${result.uid}，下次采集（run-once）起生效。`;
    form.reset();
    await refresh();
  } catch (reason) {
    error.value = String(reason);
    busy.value = false;
  }
}

function submitWatchTicker(event: Event) {
  const form = event.currentTarget as HTMLFormElement;
  action("/watchlist/add", Object.fromEntries(new FormData(form).entries()));
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

onMounted(async () => {
  applyTheme();
  await refresh();
  await restoreAutomationSettings();
  await restoreCollectStatus();
  await restoreEnrichStatus();
  if (page.value?.view === "operations") startOperationsStatusPolling();
  document.addEventListener("visibilitychange", handleVisibilityChange);
});
onBeforeUnmount(() => {
  stopCollectStatusPolling();
  stopEnrichStatusPolling();
  stopOperationsStatusPolling();
  document.removeEventListener("visibilitychange", handleVisibilityChange);
});
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
        <li><a class="nav-item" :class="{ on: navActive('operations') }" href="/?view=operations"><svg viewBox="0 0 24 24" class="ico"><path d="M4 6h16M4 12h16M4 18h16" /><circle cx="8" cy="6" r="2" /><circle cx="16" cy="12" r="2" /><circle cx="10" cy="18" r="2" /></svg>采集与富化</a></li>
        <li><a class="nav-item" :class="{ on: navActive('queue') }" href="/?view=queue"><svg viewBox="0 0 24 24" class="ico"><path d="M3 12h5l2 3h4l2-3h5" /><path d="M5 5h14v14H5z" /></svg>待处理队列</a></li>
        <li><a class="nav-item" :class="{ on: navActive('pinned') }" href="/?view=pinned"><svg viewBox="0 0 24 24" class="ico"><path d="M12 17v5" /><path d="M9 3h6l-1 6 3 3v2H7v-2l3-3-1-6z" /></svg>已钉住</a></li>
        <li><a class="nav-item" :class="{ on: navActive('raw') }" href="/?view=raw"><svg viewBox="0 0 24 24" class="ico"><path d="M4 7h16" /><path d="M4 12h16" /><path d="M4 17h10" /></svg>原始时间线</a></li>
        <li><a class="nav-item" :class="{ on: navActive('filtered') }" href="/?view=filtered"><svg viewBox="0 0 24 24" class="ico"><path d="M4 5h16l-6 7v6l-4 2v-8z" /></svg>标签过滤流</a></li>
        <li><a class="nav-item" :class="{ on: navActive('claims') }" href="/?view=claims"><svg viewBox="0 0 24 24" class="ico"><path d="M5 4h14v16H5z" /><path d="M8 9h8M8 13h5" /></svg>命题确认</a></li>
        <li><a class="nav-item" :class="{ on: navActive('decisions') }" href="/?view=decisions"><svg viewBox="0 0 24 24" class="ico"><path d="M5 4h14v16H5z" /><path d="M8 8h8M8 12h8M8 16h5" /></svg>我的决策</a></li>
        <li><a class="nav-item" :class="{ on: navActive('watchlist') }" href="/?view=watchlist"><svg viewBox="0 0 24 24" class="ico"><path d="M12 3v18M3 12h18" /><circle cx="12" cy="12" r="8" /></svg>关注列表</a></li>
        <li><a class="nav-item" :class="{ on: navActive('analysis') }" href="/?view=analysis"><svg viewBox="0 0 24 24" class="ico"><path d="M4 19V9M10 19V5M16 19v-7M22 19H2" /></svg>统计分析</a></li>
        <li><a class="nav-item" :class="{ on: navActive('frameworks') }" href="/?view=frameworks"><svg viewBox="0 0 24 24" class="ico"><path d="M4 4h7v7H4z" /><path d="M13 4h7v7h-7z" /><path d="M4 13h7v7H4z" /><path d="M13 13h7v7h-7z" /></svg>框架库</a></li>
        <li><a class="nav-item" :class="{ on: navActive('recall') }" href="/?view=recall"><svg viewBox="0 0 24 24" class="ico"><circle cx="11" cy="11" r="7" /><path d="M11 8v3l2 2M21 21l-4-4" /></svg>主题回溯</a></li>
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
        <div class="topbar-actions">
          <select v-model="theme" aria-label="主题" @change="applyTheme">
            <option value="system">跟随系统</option><option value="light">浅色</option><option value="dark">暗色</option>
          </select>
        </div>
      </header>

      <main class="content">
        <p v-if="busy && !collecting" class="notice">正在读取归档...</p>
        <p v-if="error" class="error">{{ error }}</p>

        <template v-if="page?.view === 'operations'">
          <div class="page-title"><div><h1>采集与富化</h1><p class="sub">集中执行归档更新，并查看当前进度和本次运行日志。</p></div></div>
          <section class="panel automation-panel">
            <div>
              <span class="eyebrow">自动化</span>
              <h2>采集计划</h2>
              <p class="muted">
                <template v-if="automationSettings.collection_enabled">
                  下次自动采集 {{ fmtTime(automationSettings.next_collection_at) }}
                </template>
                <template v-else>自动采集已关闭</template>
              </p>
            </div>
            <label class="toggle-row">
              <input v-model="automationSettings.collection_enabled" type="checkbox" :disabled="busy" @change="saveAutomationSettings">
              <span>自动采集</span>
            </label>
            <label class="compact-field">
              <span>采集周期（分钟）</span>
              <input v-model.number="automationSettings.collection_interval_minutes" type="number" min="5" max="10080" step="5" :disabled="busy" @change="saveAutomationSettings">
            </label>
            <label class="toggle-row">
              <input v-model="automationSettings.auto_enrich" type="checkbox" :disabled="busy" @change="saveAutomationSettings">
              <span>采集完成后自动富化</span>
            </label>
            <button :disabled="busy || automationSaving" @click="saveAutomationSettings">
              {{ automationSaving ? "保存中…" : "保存配置" }}
            </button>
            <span v-if="automationNotice" class="muted">{{ automationNotice }}</span>
          </section>
          <div class="operations-grid">
            <section class="panel operation-panel">
              <header class="operation-head">
                <div><span class="eyebrow">采集</span><h2>更新全部博主归档</h2></div>
                <button :disabled="collecting || enriching || busy" @click="runCollection">
                  {{ collecting ? "采集中…" : "立即采集" }}
                </button>
              </header>
              <div class="progress-track" :class="{ active: collecting }"><span :style="{ width: collecting ? '38%' : collectLogs.length ? '100%' : '0%' }"></span></div>
              <div class="operation-status" aria-live="polite">
                <strong>{{ collectPhase || "尚未开始采集" }}</strong>
                <span class="muted mono">{{ collectElapsed }} 秒</span>
              </div>
              <p v-if="collectNotice" class="notice">{{ collectNotice }}</p>
              <div class="run-log">
                <div class="run-log-head"><h3>运行日志</h3><span class="muted">{{ collectLogs.length }} 条</span></div>
                <p v-if="!collectLogs.length" class="muted">暂无采集日志。</p>
                <div v-for="(item, index) in collectLogs" :key="`${item.at}-${index}`" class="log-line">
                  <time>{{ fmtTime(item.at) }}</time><span>{{ item.message }}</span>
                </div>
              </div>
            </section>

            <section class="panel operation-panel">
              <header class="operation-head">
                <div><span class="eyebrow">富化</span><h2>处理博主待富化发言</h2></div>
                <span class="pill">{{ page.authors.reduce((sum: number, author: Row) => sum + Number(author.pending_enrichment_count || 0), 0) }} 条待处理</span>
              </header>
              <div class="progress-track"><span :style="{ width: `${enrichmentProgress()}%` }"></span></div>
              <div class="operation-status" aria-live="polite">
                <strong>{{ enrichPhase || "尚未开始富化" }}</strong>
                <span class="muted mono">{{ enrichProcessed }}/{{ enrichTotal }}</span>
              </div>
              <p v-if="enrichNotice" class="notice">{{ enrichNotice }}</p>
              <div class="enrich-author-list">
                <div v-for="author in page.authors" :key="author.author_platform_uid" class="enrich-author-row">
                  <AuthorBadge :item="author" />
                  <span class="muted">待富化 {{ author.pending_enrichment_count || 0 }}</span>
                  <button class="secondary" :disabled="collecting || enriching || busy || !author.pending_enrichment_count" @click="runEnrichment(author)">
                    {{ enriching && activeEnrichAuthorUid === String(author.author_platform_uid) ? "富化中…" : "开始富化" }}
                  </button>
                </div>
              </div>
              <div class="run-log">
                <div class="run-log-head"><h3>运行日志</h3><span class="muted">{{ enrichLogs.length }} 条</span></div>
                <p v-if="!enrichLogs.length" class="muted">暂无富化日志。</p>
                <div v-for="(item, index) in enrichLogs" :key="`${item.at}-${index}`" class="log-line">
                  <time>{{ fmtTime(item.at) }}</time><span>{{ item.message }}</span>
                </div>
              </div>
              <details v-if="enrichDetails.length" class="enrichment-report">
                <summary>逐条结果</summary>
                <div class="enrichment-details">
                  <div v-for="item in enrichDetails" :key="`${item.version_id}-${item.status}`" class="enrichment-detail">
                    <span class="enrichment-result" :class="item.status">{{ item.status === "success" ? "成功" : "失败" }}</span>
                    <div>
                      <a :href="`/posts/${item.post_id}`">帖子 {{ item.post_id }} · 版本 {{ item.version_id }}</a>
                      <p>{{ item.excerpt || "无正文摘要" }}</p>
                      <p v-if="item.status === 'failed'" class="enrichment-error">{{ item.error_type }}：{{ item.error }}</p>
                    </div>
                  </div>
                </div>
              </details>
            </section>
          </div>
        </template>

        <template v-else-if="page?.view === 'authors'">
          <div class="page-title"><div><h1>博主最近观点</h1><p class="sub">选择博主，查看最近市场相关观点和后续变化。</p></div></div>
          <details class="panel">
            <summary>+ 添加博主</summary>
            <form @submit.prevent="submitAddAuthor">
              <label>雪球主页 URL 或数字 uid<input name="account" placeholder="https://xueqiu.com/u/1234567890" required></label>
              <label>备注<input name="note"></label>
              <button :disabled="busy">登记博主</button>
            </form>
            <p class="muted small">仅登记到追踪列表，下次运行 run-once 起开始采集（首轮自动回填基线）。</p>
            <p v-if="addAuthorNotice" class="notice">{{ addAuthorNotice }}</p>
          </details>
          <div class="author-layout">
            <aside class="panel roster">
              <div class="roster-head"><span class="eyebrow">博主</span><small class="muted">{{ page.authors.length }} 位在档</small></div>
              <div v-for="author in page.authors" :key="author.author_platform_uid" class="author-option" :class="{ active: author.author_platform_uid === page.selected?.author_platform_uid }">
                <a class="author-pick" :href="`/?author=${encodeURIComponent(author.author_platform_uid)}`">
                  <AuthorBadge :item="author" />
                  <small class="muted">观点发言 {{ author.viewpoint_count }} · 已评估观点 {{ author.evaluated_viewpoint_count }}</small>
                  <small class="freshness" :class="{ delayed: author.pending_enrichment_count > 0 }">
                    原始 {{ fmtTime(author.latest_post_at) }} · 观点页 {{ fmtTime(author.latest_viewpoint_at) }}
                    <template v-if="author.pending_enrichment_count"> · 待富化 {{ author.pending_enrichment_count }}</template>
                  </small>
                  <small class="freshness">最近富化 {{ fmtTime(author.latest_enrichment_at) }}</small>
                </a>
                <a v-if="xueqiuUrl(author)" class="xq-jump" :href="xueqiuUrl(author)" target="_blank" rel="noopener noreferrer" title="在雪球查看主页">雪球 ↗</a>
              </div>
              <p class="roster-note muted">仅展示观点构成，不做跨博主排名或命中率评分。</p>
            </aside>
            <section class="stream">
              <div v-if="page.selected" class="author-banner">
                <AuthorBadge :item="page.selected" />
                <div class="author-actions">
                  <a v-if="xueqiuUrl(page.selected)" class="xq-jump" :href="xueqiuUrl(page.selected)" target="_blank" rel="noopener noreferrer" title="在雪球查看主页">雪球主页 ↗</a>
                </div>
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

        <template v-else-if="page?.view === 'watchlist'">
          <div class="page-title"><div><h1>关注列表</h1><p class="sub">新市场相关版本命中标的后，通过私网链接提醒。</p></div></div>
          <section class="panel">
            <h2>添加关注标的</h2>
            <form @submit.prevent="submitWatchTicker">
              <label>标的代码<input name="ticker" placeholder="SH688303" required></label>
              <label>名称<input name="name"></label>
              <label>备注<textarea name="note"></textarea></label>
              <button :disabled="busy">添加或更新</button>
            </form>
          </section>
          <section class="stream">
            <article v-for="item in page.items" :key="item.ticker" class="card">
              <header><h2>{{ item.ticker }}<span v-if="item.name"> · {{ item.name }}</span></h2><span class="pill">已提醒 {{ item.alert_count }}</span></header>
              <p class="muted">加入时间 {{ fmtTime(item.added_at) }}</p>
              <p v-if="item.note">{{ item.note }}</p>
              <button class="secondary" :disabled="busy" @click="action('/watchlist/remove', { ticker: item.ticker })">移除</button>
            </article>
            <p v-if="!page.items.length" class="empty">暂无关注标的。</p>
          </section>
        </template>

        <template v-else-if="page?.view === 'analysis'">
          <div class="page-title"><div><h1>统计分析</h1><p class="sub">仅展示分布与组成证据，不对单次事件归因。</p></div></div>
          <div class="stream-label"><span class="eyebrow">选择性删除检验</span></div>
          <section class="stream">
            <article v-for="item in page.selective_deletion" :key="`${item.author_id}-${item.horizon_days}-${item.benchmark_ticker}-${item.outcome_method_version}`" class="card">
              <header><h2>{{ item.author_name }} · {{ item.horizon_days }} 天</h2><span class="pill">{{ item.comparison_label }}</span></header>
              <p class="muted">{{ item.benchmark_ticker }} · {{ item.outcome_method_version }} · 每组门槛 {{ item.min_group_samples }}</p>
              <div class="market-row"><strong>来源页明确已移除</strong><span>样本 {{ item.removed.sample_count }}<template v-if="item.sufficient_samples"> · 中位超额 {{ percent(item.removed.median_excess_return) }} · 平均超额 {{ percent(item.removed.mean_excess_return) }}</template></span></div>
              <div class="market-row"><strong>未观察到明确移除</strong><span>样本 {{ item.retained.sample_count }}<template v-if="item.sufficient_samples"> · 中位超额 {{ percent(item.retained.median_excess_return) }} · 平均超额 {{ percent(item.retained.mean_excess_return) }}</template></span></div>
            </article>
            <p v-if="!page.selective_deletion.length" class="empty">暂无可比较的已结算命题。</p>
          </section>
          <div class="stream-label"><span class="eyebrow">跨博主拥挤事件</span></div>
          <section class="stream">
            <article v-for="event in page.crowding_events" :key="event.id" class="card">
              <header><h2>{{ event.ticker_name || event.ticker }} · {{ event.direction }}</h2><span class="pill">{{ event.author_count }} 位作者</span></header>
              <p class="muted">{{ fmtTime(event.window_start) }} 至 {{ fmtTime(event.window_end) }} · {{ event.method_version }}</p>
              <div v-for="member in event.members" :key="member.claim_id" class="market-row">
                <a :href="`/posts/${member.post_id}`">{{ member.author_name }} · 命题 {{ member.claim_id }} · 版本 {{ member.version_id }}</a>
                <span v-if="member.resolved_at">事后标的 {{ percent(member.raw_return) }} · 超额 {{ percent(member.excess_return) }}</span>
                <span v-else class="muted">尚未结算</span>
              </div>
            </article>
            <p v-if="!page.crowding_events.length" class="empty">暂无达到门槛的拥挤事件。</p>
          </section>
        </template>

        <template v-else-if="page?.view === 'frameworks'">
          <div class="page-title"><div><h1>框架库</h1><p class="sub">作者明确表达过的分析框架，逐条链回原帖版本。prompt 版本 {{ page.prompt_version }}</p></div></div>
          <div class="toolbar">
            <a :class="{ on: !page.topic }" href="/?view=frameworks">全部 {{ page.topics.reduce((sum: number, item: Row) => sum + item.count, 0) }}</a>
            <a v-for="item in page.topics" :key="item.topic" :class="{ on: page.topic === item.topic }" :href="`/?view=frameworks&topic=${encodeURIComponent(item.topic)}`">{{ item.topic }} {{ item.count }}</a>
          </div>
          <div v-if="page.variables.length" class="toolbar">
            <span class="muted small">输入变量：</span>
            <a v-for="item in page.variables.slice(0, 20)" :key="item.variable" :class="{ on: page.variable === item.variable }" :href="`/?view=frameworks&variable=${encodeURIComponent(item.variable)}`">{{ item.variable }} {{ item.count }}</a>
          </div>
          <section class="stream">
            <article v-for="item in page.items" :key="item.id" class="card">
              <header><h2>{{ item.topic }} · {{ item.conclusion_shape }}</h2><span class="pill">{{ item.author_display_name || item.author_platform_uid }}</span></header>
              <p class="muted">版本 {{ item.version_id }} · 首次观察 {{ fmtTime(item.version_first_observed_at) }} · {{ item.source_status_label }}</p>
              <p v-if="!item.source_readable" class="error">原帖当前不可读，以下框架来自首次观察时的存档版本。</p>
              <p>{{ item.summary }}</p>
              <p><b>输入变量：</b><span v-for="name in item.input_variables" :key="name" class="pill">{{ name }}</span></p>
              <h3>逻辑链</h3><pre>{{ item.logic_chain }}</pre>
              <p v-if="item.applicability_conditions"><b>作者声明的适用条件：</b>{{ item.applicability_conditions }}</p>
              <p v-if="item.invalidation_conditions"><b>作者声明的失效条件：</b>{{ item.invalidation_conditions }}</p>
              <blockquote>{{ item.evidence_snippet }}</blockquote>
              <details><summary>查看存档原文</summary><pre>{{ item.content_text }}</pre></details>
              <p><a :href="`/posts/${item.post_id}`">查看版本证据</a></p>
            </article>
            <p v-if="!page.items.length" class="empty">暂无已抽取的分析框架。先运行 extract-frameworks。</p>
          </section>
        </template>

        <template v-else-if="page?.view === 'recall'">
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
          <section>
            <div class="stream-label"><span class="eyebrow">该作者与本帖标的</span></div>
            <p v-if="page.card.ticker_history.empty_label" class="empty">{{ page.card.ticker_history.empty_label }}</p>
            <article v-for="item in page.card.ticker_history.items" :key="`${item.version_id}-${item.ticker}`" class="card">
              <header><h3>{{ item.ticker }} · 版本 {{ item.version_id }}</h3><span class="pill">{{ item.has_removal_event ? "来源页曾明确已移除" : item.source_state }}</span></header>
              <p class="muted">首次观察 {{ fmtTime(item.first_observed_at) }} · <a :href="`/posts/${item.post_id}`">查看帖子证据</a></p>
              <pre>{{ item.content_text }}</pre>
              <div v-if="item.market_snapshot" class="market-row"><strong>描述性市场变化</strong><span>标的 {{ percent(item.market_snapshot.raw_return) }} · 超额 {{ percent(item.market_snapshot.excess_return) }}</span></div>
              <p v-for="event in item.events" :key="`${event.detected_at}-${event.dimension}`" class="muted">{{ event.detected_at }} · {{ event.dimension }}：{{ event.from_value || "无" }} → {{ event.to_value }}</p>
            </article>
          </section>
          <section><div class="stream-label"><span class="eyebrow">观察版本</span></div><article v-for="version in page.card.versions" :key="version.version_id" class="card"><h3>观察版本 {{ version.version_id }}</h3><p class="muted">首次 {{ fmtTime(version.first_observed_at) }} · 最后 {{ fmtTime(version.last_observed_at) }}</p><pre>{{ version.content_text }}</pre><details><summary>相对上一版本 diff</summary><pre>{{ version.diff_from_prior_observed_version || "首个观察版本" }}</pre></details></article></section>
          <section v-for="name in ['feed_observations', 'direct_probes', 'events', 'attention_log', 'rewrite_exercises', 'enrichments']" :key="name"><div class="stream-label"><span class="eyebrow">{{ name }}</span></div><pre class="data">{{ JSON.stringify(page.card[name], null, 2) }}</pre></section>
        </template>
      </main>
    </div>
  </div>
</template>
