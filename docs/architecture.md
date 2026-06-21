# 架构与数据模型

本文件承接 `tasks.md` 中较稳定的技术栈、数据模型和状态机细节。日常任务先看 `tasks.md`，需要改采集、存档、状态机、数据库或适配器时再读本文件。

## 技术栈与部署

第一版目标：一个平台、一个进程、一个 SQLite、可恢复备份、证据卡片、自省日志、改写训练。

| 层 | 选型 | 说明 |
|---|---|---|
| 运行形态 | 单 Python 进程 | 不引入容器编排、消息队列、独立 worker |
| 数据库 | SQLite + WAL | 每个连接执行 `PRAGMA foreign_keys=ON`，写入包进单一事务 |
| 追加写与身份保护 | DB 触发器 | 六张证据表拦截 `UPDATE / DELETE`，`posts` 禁删且身份字段不可改 |
| JSON 列 | `TEXT CHECK(col IS NULL OR json_valid(col))` | SQLite 无 jsonb |
| 调度 | cron 或 APScheduler | feed 轮询、直链复查两个独立任务 |
| 采集 | Python + httpx / playwright | 平台适配器实现 `NormalizedPost` |
| 富化 | LLM API | 富化结果不参与市场结果判定 |
| 界面 | Vue 本地页面 + CLI | Vue 构建产物由 Python 单进程托管 |
| 凭据 | 环境变量或被忽略的本地配置 | 绝不入库、导出、日志、异常文本 |
| 备份 | SQLite backup API 或 `VACUUM INTO` | 不直接复制主库文件，定期恢复验证 |
| 远程访问 | Tailscale 私网 | 默认只监听 `127.0.0.1`，不暴露公网 |

迁移到 Postgres、Redis、worker 或 FastAPI 仅在出现实际瓶颈时评估。页面维护成本已构成实际瓶颈，因此界面改用 Vue，继续复用现有 Python 服务层与单进程部署。

## 核心状态

```
feed_state:    present | absent_confirmed | out_of_scope | unknown
source_state:  reachable | gone_confirmed | unavailable | unknown
watch_mode:    recent_window | pinned | inactive
```

UI 由三者组合成人读状态：

1. `present` 表示在场。
2. `gone_confirmed` 表示来源页明确显示已移除，不归因主体，属强信号。
3. `unavailable` 表示直链复查当前不可访问，无法确认移除，属弱信号。
4. `out_of_scope + inactive` 表示已归档且停止监控（复查生命周期结束或确认移除）。

`absent_confirmed` 为历史枚举值，feed 侧负面推断退役后不再新增；删帖弱/强信号统一来自 Track B 直链复查的 `unavailable` / `gone_confirmed`。

## 表结构草案

```
authors
  id, platform, platform_uid, ..., live_monitoring_started_at, notes
  UNIQUE(platform, platform_uid)

fetch_runs                       [append-only]
  id, author_id, platform, started_at, finished_at,
  status(ok|partial|failed), login_state(valid|expired|unknown),
  pages_fetched, pagination_complete(bool), covered_from, covered_to,
  rate_limited(bool), http_error_count, ingest_mode(live|backfill),
  adapter_version, parse_failure_count, reached_timeline_end(bool), notes
  -- 完整健康 = status=ok AND login_state=valid AND pagination_complete AND NOT rate_limited
  -- reached_timeline_end：本轮翻到时间线尽头，区别于仅覆盖近窗或到 until
  -- parse_failure_count：本轮降级或无法解析的条目数，>0 视为不干净

probe_runs                       [append-only]
  id, post_id, started_at, finished_at, observed_at,
  status(ok|partial|failed), http_status, login_state, rate_limited(bool),
  result(reachable|explicitly_removed|restricted|not_found|unknown),
  content_fidelity(full|preview|na),
  observed_version_id nullable FK -> post_versions.id,
  ingest_mode(live|backfill), adapter_version, notes
  -- 健康 = status=ok AND login_state=valid AND NOT rate_limited

post_observations                [append-only]
  id, fetch_run_id, post_id, observed_at, present(bool),
  content_hash nullable, content_fidelity(full|preview|na),
  version_id nullable FK -> post_versions.id
  UNIQUE(fetch_run_id, post_id)

posts        (稳定身份登记 + 可变投影；禁删；身份字段不可改)
  id, author_id, platform, platform_post_id, first_seen_at,
  last_present_at, current_version_id FK, current_content_hash, current_image_manifest_hash,
  absent_healthy_streak(int default 0),
  feed_state, source_state, source_checked_at(nullable), watch_mode,
  posted_at_claimed, url, ingest_mode(live|backfill),
  raw_meta TEXT CHECK(raw_meta IS NULL OR json_valid(raw_meta))
  UNIQUE(platform, platform_post_id)

post_versions                    [append-only]
  id, post_id, content_text, content_hash, image_manifest_hash,
  first_observed_at, ingest_mode(live|backfill), raw_payload
  -- full 内容一变即新建行，preview 不建版本

post_events                      [append-only]
  id, post_id, dimension(feed_state|source_state|watch_mode|content),
  from_value, to_value, detected_at,
  evidence_fetch_run_id(nullable), evidence_probe_run_id(nullable),
  from_version_id(nullable), to_version_id(nullable), notes

recheck_queue
  id, post_id, reason(llm_candidate|recent_feed_absent),
  enqueued_at, expires_at, state(pending|confirmed|expired)
  -- 部分唯一索引：UNIQUE(post_id) WHERE state='pending'

attention_log
  id, author_id, post_id(nullable), version_id(nullable),
  triggered_at, my_reason, my_expectation, reviewed_at, my_retro

rewrite_exercises
  id, post_id, version_id, original_text, llm_rewritten_claim, llm_rationale,
  model, prompt_version, my_verdict(valid|too_vague|wrong), created_at

enrichments
  id, post_id, version_id, post_type,
  label_first_hand_info(bool), label_transferable_framework(bool),
  label_reasoned_non_consensus(bool), is_market_related(bool),
  rationale, evidence_snippet, model, prompt_version, created_at
  UNIQUE(version_id, prompt_version)

post_images                     [append-only]
  id, version_id, source_url, normalized_url, ordinal,
  sha256(nullable), mime_type(nullable), byte_size(nullable), image_bytes(nullable),
  downloaded_at, download_status(ok|failed), notes

image_ocr
  id, image_id, image_sha256, engine, engine_version, ocr_text, created_at
  UNIQUE(image_id, engine, engine_version)

image_enrichments
  id, image_id, image_sha256, model, prompt_version, prompt, description, created_at
  UNIQUE(image_id, model, prompt_version)

claims
  id, post_id, version_id, author_id, ticker, direction(long|short|neutral),
  horizon_days(nullable), target_price(nullable), confidence_phrasing,
  claim_made_at, ingest_mode,
  status(open|expired|resolved), created_at

claim_outcomes
  claim_id, resolved_at, raw_return, benchmark_return, excess_return,
  outcome_method_version, notes

prices
  ticker, date, close, ...
```

## Track A：feed 轮询

每次轮询写一条 `fetch_run`，记录 `status` 与覆盖范围。轮询是增量的：翻页只翻到与上一轮已采集内容重合（覆盖回 `previous_covered_to`）即停，例行 15 分钟轮询通常只取 1 页。`monitoring.window_days` 仅在某博主第一次实时轮询、还没有可重合的锚点时作为初始种子深度。

1. 正面存档按调度和预算规则执行。run-once 只采集已到期博主；第 1 页首页指纹未变时，每天最多完整观察首页一次，其余轮次记录轻探测 run 并停止翻页。稳定旧帖仅在 `positive_observation_interval_days` 后进入完整观察，达到 `positive_observation_max_count` 后跳过完整观察。新帖、内容或图片变化、解析降级、状态恢复会写 `present=true` 的 `post_observations`。
2. feed 不做负面推断。增量轮询的覆盖范围只是「自上次以来的新内容」，不再横跨监控窗口，因此本轮未见到某帖不构成缺席信号。删帖与编辑的发现改由 Track B 的按帖直链复查承担。`absent_confirmed`、`absent_healthy_streak`、feed 侧 `out_of_scope` 不再由 feed 产生（枚举值保留以兼容历史行）。`recent_feed_absent` 不再入队。
3. partial 或 failed run 与 healthy run 在 feed 侧行为一致，都只做正面存档。账号采集健康度仍由近 K 条 `fetch_runs` 推导。reconnect 失败（覆盖未回到 `previous_covered_to`）时该 run 降级为 partial，记 `coverage_gap`。增量锚点 `last_live_covered_to` 只取健康 live run（ok、valid、paginated complete、未限流）的 `MAX(covered_to)`：`coverage_gap` 的 partial run 只抓到较新几页、未与既有覆盖连续，其 `covered_to` 不得推进锚点，否则下一轮会从这个新锚点起翻、把中间漏掉的帖子永久落下；保留上一个已确认锚点后，下一轮会重新翻回它、补回缺口。

## Track B：直链复查（含按帖复查生命周期）

对象有三类，按 post id 去重：`watch_mode=pinned` 帖（每轮复查，人工置顶不自动退场）、`recheck_queue` 中 `state=pending` 帖（如 LLM 候选，`recent_feed_absent` 历史 TTL 仍到期转 `expired`）、以及处在复查生命周期内、到期应复查的 `recent_window` 帖。

复查生命周期由追加型证据派生，无需队列行：一条 `recent_window` 帖在其声称时间后 `first_recheck_after_days`（默认 1 天）首检，之后每 `positive_observation_interval_days`（默认 7 天，从上次健康复查算起）复查一次，累计 `positive_observation_max_count`（默认 5 次）健康复查后退场。某帖一旦 `gone_confirmed`，或满次，置 `watch_mode=inactive` 且 `feed_state=out_of_scope`（已归档、停止监控，宪章第 8 条）；置顶帖是人工覆盖，永不自动退场。「今天 12 点发帖，次日 12 点首检，第 8 天第二次复查」即由此而来。

每轮直链复查有预算上限 `polling.max_probes_per_run`（默认 40）：每次复查是一次较慢的浏览器往返，启用生命周期后大量历史帖可能同时到期（尤其首轮），预算把单轮时长收住，让积压跨轮排空。`probe_targets` 按优先级出队——置顶帖与队列帖在前（预算不会饿死它们），生命周期积压在后并按最久未复查优先（`COALESCE(上次健康复查, 声称时间, 首见时间)` 升序），因此截断仍优先处理等待最久的帖、跨轮公平轮转。

健康门：只有 `probe_run.status=ok`、`login_state=valid`、`rate_limited=false` 时，才推进 `source_state`、更新 `source_checked_at`、捕获版本。退化复查只留 `probe_run` 痕迹并提示账号异常，不改帖子状态。

健康会话按 `result` 推进：

1. `reachable` 且 `content_fidelity=full`：置 `source_state=reachable`，解析正文并写版本，`probe_runs.observed_version_id` 指向所见版本，内容变化写 `content` 事件。
2. `reachable` 且 `content_fidelity=preview`：只更新可访问性，不建版本。
3. `explicitly_removed`：置 `source_state=gone_confirmed`。
4. `restricted / not_found`：置 `source_state=unavailable`。
5. `gone_confirmed` 具有黏性，其后 `restricted / not_found` 只追加复查证据，不降级；仅 `reachable` 可翻回。

## 内容版本处理

1. `content_fidelity=preview` 的观察只记 observation 或 probe 证据，`version_id=null`，不建版本、不触发 content 事件。
2. full 内容与 `current_content_hash` 一致时不新增版本。
3. full 内容与 `current_content_hash` 不一致时追加新 `post_version`，更新 `current_version_id/hash`，写 `content` 事件。回放到旧内容也新建版本。

## 末次观察推导

视图：

```
version_sightings(version_id, observed_at, channel, run_id)
```

来源：

1. feed：`SELECT version_id, observed_at, 'feed', fetch_run_id FROM post_observations WHERE version_id IS NOT NULL`
2. 直链：`SELECT observed_version_id, observed_at, 'direct', id FROM probe_runs WHERE observed_version_id IS NOT NULL`

某版本末次观察时间为 `MAX(observed_at) FROM version_sightings WHERE version_id=?`。被节流跳过的重复 feed sighting 不进入该视图。

## 钉住与队列

1. 自动钉住来源：手动钉、写 `attention_log`、做 `rewrite_exercise`、手动确认为可证伪命题。
2. 取消钉住后，仍在近期窗口内回到 `recent_window`，已滑出则转 `inactive`。
3. LLM 候选入 `recheck_queue`，用户确认后转 `pinned` 和 `confirmed`，TTL 到期转 `expired`。
