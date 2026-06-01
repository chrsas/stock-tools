# KOL 发言追踪工具 开发规范 v7（冻结候选，供 Agent 执行）

> 本文件是交给编码 Agent 的执行规范。Agent 必须先通读「第 0 节 项目宪章」并全程遵守，这些是不可违反的硬约束，优先级高于任何后续任务的便利性。
>
> **证据与身份的边界**：`fetch_runs / probe_runs / post_observations / post_versions / post_events` 是不可篡改的证据，只追加。`posts` 承担**双重身份**：它的身份字段是被证据表外键引用的稳定登记（不可删、不可改），其余字段是可由证据重算的可变投影。

---

## 0. 项目宪章（不可违反的硬约束）

工具定位是「照妖镜 + 注意力分配器」，识别谁在装、谁在耍赖，并把高价值内容捞给用户。它**不是**预测、跟单或排行榜工具。核心机制：feed 轮询负责发现新帖与近期缺席；直链复查负责持续盯住重要证据（可访问性与编辑）；钉住动作决定哪些历史证据值得长期投入采集成本。

1. **不可篡改证据只追加。** 五张证据表由 DB 触发器强制只追加。**`posts` 禁止物理删除**，且身份字段 `id, author_id, platform, platform_post_id, first_seen_at` 锁成不可更新（触发器拦截 UPDATE/DELETE）；其余字段可更新。
2. **一次采集完成后的所有写入在同一 SQLite 事务内原子提交，崩溃整体回滚。**
   - feed 侧：`insert fetch_run → insert posts 空壳(新帖) → insert post_versions(full 内容变化) → insert post_observations → insert events → update posts 投影`。
   - 直链侧：`内存解析 → (full 内容变化)insert post_version → insert probe_run(直接带 observed_version_id) → insert events → update posts 投影`。先有版本行，probe_run 的外键才成立。
3. **`first_seen_at` 与各 `first_observed_at` 写一次即不可变。** 不存可变 `last_observed_at`，由 `version_sightings` 视图聚合推导（见 2.5）。
4. **只能保存系统实际观察到的版本。** 界面一律用「首次观察 / 最后观察 / 检测到缺失」表述，禁止暗示精确删除或修改时刻。
5. **状态是三个正交维度，不混用。** `edited` 不是状态，是历史事件。
6. **健康门只限制负面推断，不阻止正面存档。** 成功解析出的在场帖子（无论本轮是否完整健康）始终保存 observation、版本与原始载荷并更新在场投影；只有完整健康 run 才允许生成缺席 observation、累计 streak、判定 out_of_scope（见 2.2）。
7. **证据层对删帖主体不下任何断言。** `source_state=gone_confirmed` 仅表示「来源页明确显示已移除」，无法区分作者删除、平台审核或其他原因；裸 404、跳转、限权记 `unavailable`。
8. **未钉住且滑出监控窗口的帖子停止持续监控。** 存档保留，但不再承诺发现其后续删除或编辑。
9. **历史回填与实时监控按版本层分离。** 仅 `ingest_mode='live'` 且 `first_observed_at >= live_monitoring_started_at` 的版本对应命题可进入事件研究。
10. **`claims` 只收原帖明确表达过的可证伪内容；`claim_made_at` 取对应版本首次观察时间。** LLM 改写产物只进 `rewrite_exercises`，禁止流入回测。`horizon_days`、`target_price` 允许为空，LLM 不得补造。
11. **LLM 只做富化，GUI 只把证据递到用户面前；无跨人排行榜；结论行挂 `version_id` 外键；命中率类指标禁止头部展示。工具只做减法，每阶段独立可用；本地单用户、不对外发布、不暴露公网、温和采集。**

---

## 1. 技术栈与部署（第一版刻意从简）

第一版目标：**一个平台、一个进程、一个 SQLite、可恢复备份、证据卡片、自省日志、改写训练。**

| 层 | 第一版选型 | 说明 |
|---|---|---|
| 运行形态 | 单 Python 进程 | 不引入容器编排、消息队列、独立 worker |
| 数据库 | SQLite + WAL | 每个连接执行 `PRAGMA foreign_keys=ON;`；完成写入包进单一事务 |
| 追加写/身份保护 | DB 触发器 | 五张证据表 `BEFORE UPDATE/DELETE` 即 `RAISE`；`posts` 禁删且身份字段不可改；人工纠错走单独审计通道 |
| JSON 列 | `TEXT CHECK(col IS NULL OR json_valid(col))` | SQLite 无 jsonb |
| 调度 | cron 或 APScheduler | feed 轮询、直链复查两个独立任务 |
| 采集 | Python + httpx / playwright | 单平台适配器，实现 `NormalizedPost` |
| 富化 | LLM API | 阶段 2 仅手动触发单条；阶段 3 才批量 |
| 界面 | 本地页面或 CLI | 第一版不上 React |
| 凭据 | API 密钥仅环境变量；登录 cookie 可用环境变量或被忽略的本地配置 | 绝不入库、绝不进导出文件 |
| 备份 | SQLite backup API 或 `VACUUM INTO` | 不可直接 cp 主文件；定期恢复验证 |
| 远程 | 暂不做 | 真需手机访问时再配 Tailscale |

迁移到 Postgres/Redis/worker/FastAPI/React 仅在出现实际瓶颈时。

---

## 2. 数据模型

### 2.1 三个正交状态维度（存于 posts，变迁写 post_events）

```
feed_state:    present | absent_confirmed | out_of_scope | unknown
source_state:  reachable | gone_confirmed | unavailable | unknown
watch_mode:    recent_window | pinned | inactive
```

UI 由三者组合成人读状态：`present`=在场；`absent_confirmed + source unknown`=feed 内连续缺席未经直链确认（弱信号）；`gone_confirmed`=来源页明确显示已移除（不归因主体）；`unavailable`=直链当前不可访问；`out_of_scope + inactive`=已归档停止监控。

```
authors
  id, platform, platform_uid, ...,  live_monitoring_started_at, notes
  UNIQUE(platform, platform_uid)

fetch_runs                       [append-only]
  id, author_id, platform, started_at, finished_at,
  status(ok|partial|failed), login_state(valid|expired|unknown),
  pages_fetched, pagination_complete(bool), covered_from, covered_to,
  rate_limited(bool), http_error_count, ingest_mode(live|backfill),
  adapter_version, notes
  -- 完整健康 = status=ok AND login_state=valid AND pagination_complete AND NOT rate_limited

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
  content_hash, content_fidelity(full|preview|na),
  version_id nullable FK -> post_versions.id    -- 缺席或 preview 时为空
  UNIQUE(fetch_run_id, post_id)

posts        (稳定身份登记 + 可变投影；禁删；身份字段不可改)
  id, author_id, platform, platform_post_id, first_seen_at,   -- 身份字段，锁定
  last_present_at, current_version_id FK, current_content_hash,
  absent_healthy_streak(int default 0),
  feed_state, source_state, source_checked_at(nullable), watch_mode,
  posted_at_claimed, url, ingest_mode(live|backfill),
  raw_meta TEXT CHECK(raw_meta IS NULL OR json_valid(raw_meta))
  UNIQUE(platform, platform_post_id)

post_versions                    [append-only]
  id, post_id, content_text, content_hash,
  first_observed_at(不可变), ingest_mode(live|backfill), raw_payload
  -- 不去重：full 内容一变即新建行（含回放到旧内容也新建），保留各自首次观察时间
  -- 仅 full fidelity 的内容进入此表；preview 不建版本

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

rewrite_exercises                -- 训练材料，禁止流入回测
  id, post_id, version_id, original_text, llm_rewritten_claim, llm_rationale,
  model, prompt_version, my_verdict(valid|too_vague|wrong), created_at

enrichments
  id, post_id, version_id, post_type,
  label_first_hand_info(bool), label_transferable_framework(bool),
  label_reasoned_non_consensus(bool),
  rationale, evidence_snippet, model, prompt_version, created_at
  UNIQUE(version_id, prompt_version)
  -- 后期加 attention_tier(0..3)，需 author 富化帖数 >= MIN_SAMPLES

-- 阶段 4（占位）
claims
  id, post_id, version_id, author_id, ticker, direction(long|short|neutral),
  horizon_days(nullable), target_price(nullable), confidence_phrasing,
  claim_made_at(=对应版本 first_observed_at), ingest_mode(=对应版本),
  status(open|expired|resolved), created_at
claim_outcomes  claim_id, resolved_at, raw_return, benchmark_return, excess_return, notes
prices  (只读)  ticker, date, close, ...
```

### 2.2 Track A：feed 轮询（驱动 feed_state）

每次轮询写一条 `fetch_run`（含 `status` 与覆盖范围）。然后：
- **正面存档（始终执行，不受本轮健康度限制）**：对本轮成功解析出的每个在场帖子，写 `present=true` 的 `post_observations`（带 `content_fidelity`），按 2.4 走版本流程，更新 `last_present_at`、`feed_state=present`、`absent_healthy_streak=0`、`current_version_id/hash`。新帖先插 posts 空壳。
- **负面推断（仅完整健康 run 才执行）**：对覆盖范围内、本轮未见到的帖子，写 `present=false` 的 observation，`absent_healthy_streak += 1`，达阈值 N（可配，≥3）则 `feed_state=absent_confirmed` 并入 `recheck_queue`（`reason=recent_feed_absent`，带 TTL）写事件；对早于 `covered_from` 的帖子置 `feed_state=out_of_scope`，若 `watch_mode != pinned` 则 `watch_mode=inactive`。
- partial/failed run 只做正面存档，不做任何负面推断；账号采集健康度由近 K 条 `fetch_runs` 推导。

### 2.3 Track B：直链复查（驱动 source_state 与编辑发现）

- 对象 = `watch_mode=pinned` 帖 + `recheck_queue` 中 `state=pending` 帖（`llm_candidate` 与 `recent_feed_absent`）。`recent_feed_absent` 由 TTL 定义「近期」，到期 `expired` 退出。
- **健康门：仅当 `probe_run` 为 `status=ok` 且 `login_state=valid` 且 `rate_limited=false` 时才推进 `source_state`、更新 `source_checked_at`、捕获版本。** 退化复查只留 `probe_run` 痕迹并提示账号异常，不改帖子状态。（此处不比照 Track A 放宽：退化直链会话拿到的页面很可能是登录墙或错误页伪装，不可信。）
- 健康会话按 `result`：
  - `reachable` 且 `content_fidelity=full` → `source_state=reachable`；解析正文走 2.4，`probe_runs.observed_version_id` 指向所见版本，内容变化写 `content` 事件（`evidence_probe_run_id`）。`preview` 则只更可访问性，不建版本。
  - `explicitly_removed` → `source_state=gone_confirmed`。
  - `restricted` / `not_found` → `source_state=unavailable`。
  - **gone_confirmed 黏性**：一旦为 `gone_confirmed`，其后再出现 `restricted/not_found` 只追加复查证据，**不降级**；仅 `reachable` 才能将其翻回。

### 2.4 内容版本处理（Track A 在场、Track B 健康 reachable 共用，仅 full fidelity）

- `content_fidelity=preview` 的观察：记为 observation/probe 证据（`version_id=null`），**不建版本、不触发 content 事件**。
- `full` 内容与 `current_content_hash` 一致 → 不新增版本。
- `full` 内容与 `current_content_hash` 不一致 → 永远追加新 `post_version`（含回放旧内容也新建），更新 `current_version_id/hash`，写 `content` 事件（feed 挂 `evidence_fetch_run_id`，直链挂 `evidence_probe_run_id`）。

### 2.5 末次观察推导（合并双轨）

建视图 `version_sightings(version_id, observed_at, channel, run_id)`：
- feed：`SELECT version_id, observed_at, 'feed', fetch_run_id FROM post_observations WHERE version_id IS NOT NULL`
- 直链：`SELECT observed_version_id, observed_at, 'direct', id FROM probe_runs WHERE observed_version_id IS NOT NULL`

某版本末次观察 = `MAX(observed_at) FROM version_sightings WHERE version_id=?`。所有末次观察时间统一从此视图计算。

### 2.6 钉住与队列

- 自动钉住（`watch_mode=pinned` 写事件）：手动钉、写 `attention_log`、做 `rewrite_exercise`、手动确认为可证伪命题。
- 取消钉住：仍在近期窗口回 `recent_window`，已滑出转 `inactive`。
- LLM 候选入 `recheck_queue`（`llm_candidate`），用户确认转 `pinned`、`confirmed`，TTL 到期 `expired`。

---

## 3. 分阶段开发计划

### 阶段 1：平台探针 + 本地追加存档 + 备份导出

**1a 平台探针（先做，产出书面决策记录参数化后续）**
实测并记录：稳定持久帖子 ID；分页边界与 `covered_from/covered_to` 计算法；登录态维持与失效表现；编辑暴露方式；feed 中删帖表现；直链复查能否区分 `explicitly_removed / not_found / restricted / reachable`；直链正文能否稳定提取；**feed 与直链的正文能否归一化为相同内容**（不能则按轨标注 `content_fidelity`，feed 摘要型时编辑检测退化为仅 Track B 能力，须写明）；两轨各自频率限制；监控窗口默认天数；旧帖与钉住帖复查策略。**探针结论落地、参数回填后方可动 1b。**

**1b 存档与双轨状态机**
- 单进程 + SQLite(WAL)，连接级 `PRAGMA foreign_keys=ON`；建全部表、视图、UNIQUE/部分唯一索引、FK。
- 触发器：五张证据表只追加；`posts` 禁删与身份字段不可改。
- feed 与直链两任务，各按第 2 节事务顺序原子写入；实现 2.2–2.6 全部规则。

**1c 备份与导出**
SQLite backup API 或 `VACUUM INTO` 定时多份快照，定期恢复验证；JSON/CSV 导出；API 密钥仅环境变量，登录 cookie 可用环境变量或被忽略的本地配置，任何凭据绝不进导出文件。

**DoD**：A→B→A 落三条版本行且各带不同首次观察时间；partial run 中已见在场帖仍存档、但不产生缺席推断；full/preview 正确区分，preview 不触发编辑事件；feed 连续完整健康缺席落 `absent_confirmed` 并入队；健康直链删除占位落 `gone_confirmed` 且后续 404/限权不降级，正文被改可捕获为新版本；退化抓取/复查不动帖子状态与 `source_checked_at`；同一帖至多一条 pending；崩溃注入下事务整体回滚、证据与投影不错位；`posts` 身份字段与删除被触发器拒绝；备份可恢复且经验证。

---

### 阶段 2：原始时间线 + 证据卡片 + 关注理由 + 改写训练
不建批量富化。时间线（三维状态人读标签、删帖强弱信号分级）；证据卡片（无 LLM，单帖观察历史、版本 diff、变迁及证据 run、附注、钉住开关）；`attention_log`（锁 `version_id`、创建即自动钉住）；改写训练（按需单条 LLM 改写写 `rewrite_exercises` 含 `version_id`、创建即自动钉住）。
**DoD**：原始流与诚实观察时间可见；钉/取消钉遵守 2.6；写理由或改写训练自动钉住并锁版本。

### 阶段 3：LLM 标签 + 标签门过滤（需累积样本）
批量富化每个 `version` 出 `post_type`、三布尔标签、`rationale`、`evidence_snippet`、`model`、`prompt_version`（幂等键 UNIQUE(version_id, prompt_version)）；命中任一标签进过滤流按时间排序，过滤为默认、原始流一键可达；后期达 `MIN_SAMPLES` 加 `attention_tier`，按 `prompt_version` 隔离。
**DoD**：命中标签进过滤流；原始流始终可达；样本不足不强行分级。

### 阶段 4：事件研究等（占位，按需）
行情对齐、交易日规则、基准选择、多时间窗口待定。可能冲突候选只摆证据；`claims`+`claim_outcomes`（无 hit 头部、不跨人排名）；事件研究 pandas 自写，导出只消费对应版本 `ingest_mode='live'` 且 `first_observed_at >= live_monitoring_started_at` 的命题，回填排除；回测库到时再评估。

---

## 4. 给 Agent 的全局提醒
- 产出永远是把证据递到用户面前，绝不替用户判断。
- 过滤是默认视图但非唯一视图，原始时间线一键可达，防信息茧房。
- 不替用户写摘要让其只读摘要；原文始终可读。
- 阶段 1–2 跑通已是大部分价值；任意阶段可作合理终点。
- 全平台、全自动、精确归因、跨人评分回报曲线是凹的，非明确要求不主动扩张。

---

## 5. 用户开工前填写的配置
- 探针选定平台；追踪账号与轮询频率初值。
- 监控窗口天数、缺席阈值 N、直链复查频率、`recent_feed_absent` 与 `llm_candidate` 的 TTL、`MIN_SAMPLES`、各轨 `content_fidelity`（多数由探针建议）。
- `live_monitoring_started_at` 系统自动写入。
- LLM 供应商/模型/API Key（环境变量，阶段 2 起）。
- 行情数据源（阶段 4）。
- 部署机器；如需远程再配 Tailscale。
