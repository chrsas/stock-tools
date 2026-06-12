# KOL 发言追踪工具 开发规范 v9（冻结候选，供 Agent 执行）

> v9 变更：新增宪章第 12 条（删帖动机中性）；新增阶段 5–11（自我决策日志、中性变更摘要与通知、
> lite 结算闭环、关注列表提醒、统计分析层、框架提取、轻量多源信源层），并冻结 UI 视觉迭代。
> 阶段 1/2/2b/3 与图片证据扩展均已完成，阶段 4 由阶段 7 的 lite 路径渐进实现。
> 多平台采用**双层定位**：重装存证（双轨监控、删帖检测、结算）只限雪球；其他平台走阶段 11
> 的轻量摄入层，永不进负面推断与战绩。

> 本文件是交给编码 Agent 的执行规范。Agent 必须先通读「第 0 节 项目宪章」并全程遵守，这些是不可违反的硬约束，优先级高于任何后续任务的便利性。
>
> **证据与身份的边界**：`fetch_runs / probe_runs / post_observations / post_versions / post_events / post_images`
> 是不可篡改的证据，只追加。`posts` 承担**双重身份**：它的身份字段是被证据表外键引用的稳定登记
> （不可删、不可改），其余字段是可由证据重算的可变投影。

---

## 0. 项目宪章（不可违反的硬约束）

工具定位是「照妖镜 + 注意力分配器」，识别谁的公开观点经得起后续市场检验、谁在反复改口，并把高价值内容捞给用户。工具允许基于可追溯证据提供博主级战绩摘要、比较和排序，用于决定关注顺序；不得包装成收益承诺、自动跟单信号或投资建议。核心机制：feed 轮询负责发现新帖与近期缺席；直链复查负责持续盯住重要证据（可访问性与编辑）；钉住动作决定哪些历史证据值得长期投入采集成本。

1. **不可篡改证据只追加。** 六张证据表由 DB 触发器强制只追加。**`posts` 禁止物理删除**，且身份字段 `id, author_id, platform, platform_post_id, first_seen_at` 锁成不可更新（触发器拦截 UPDATE/DELETE）；其余字段可更新。
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
11. **LLM 只做富化，不参与判定市场结果。GUI 可以展示由已结算命题派生的博主级摘要、筛选和跨人排序，但每项指标必须同时展示样本量、统计时间窗、口径版本、未结算数量，并可下钻到对应 `version_id`、命题和市场结果。禁止隐藏亏损样本、混用不可比口径、用小样本强行排名或生成不透明综合分。工具每阶段独立可用；本地单用户、不对外发布、不暴露公网、温和采集。**
12. **呈现层对删帖动机与证据层同样保持中性。** 中国监管环境下删帖多为自保、防炫富或日常清理，单次删帖不可定性为「改口」。摘要、周报、UI、通知一律使用中性事实陈述（「删除/编辑/图片变更」），禁止「改口」「心虚」等归因措辞。区分动机只允许走分布层面的版本化统计：选择性检验（被删帖 vs 保留帖事后表现分布）、跨账号同步删帖潮（标注为平台级事件，不记到个人）、删图不删文模式。统计结论必须展示样本量，样本不足只显示「不足」。

---

## 1. 技术栈与部署（第一版刻意从简）

第一版目标：**一个平台、一个进程、一个 SQLite、可恢复备份、证据卡片、自省日志、改写训练。**

| 层 | 第一版选型 | 说明 |
|---|---|---|
| 运行形态 | 单 Python 进程 | 不引入容器编排、消息队列、独立 worker |
| 数据库 | SQLite + WAL | 每个连接执行 `PRAGMA foreign_keys=ON;`；完成写入包进单一事务 |
| 追加写/身份保护 | DB 触发器 | 六张证据表 `BEFORE UPDATE/DELETE` 即 `RAISE`；`posts` 禁删且身份字段不可改；人工纠错走单独审计通道 |
| JSON 列 | `TEXT CHECK(col IS NULL OR json_valid(col))` | SQLite 无 jsonb |
| 调度 | cron 或 APScheduler | feed 轮询、直链复查两个独立任务 |
| 采集 | Python + httpx / playwright | 单平台适配器，实现 `NormalizedPost` |
| 富化 | LLM API | 阶段 2 仅手动触发单条；阶段 3 才批量 |
| 界面 | Vue 本地页面 + CLI | Vue 构建产物由现有 Python 单进程托管；CLI 保留为维护与故障排查入口 |
| 凭据 | API 密钥仅环境变量；登录 cookie 可用环境变量或被忽略的本地配置 | 绝不入库、绝不进导出文件 |
| 备份 | SQLite backup API 或 `VACUUM INTO` | 不可直接 cp 主文件；定期恢复验证 |
| 远程 | 阶段 2b 按需启用 | 手机访问只走 Tailscale 私网，不暴露公网 |

迁移到 Postgres/Redis/worker/FastAPI 仅在出现实际瓶颈时。页面维护成本已构成实际瓶颈，
因此界面改用 Vue，继续复用现有 Python 服务层与单进程部署。

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
  adapter_version, parse_failure_count, reached_timeline_end(bool), notes
  -- 完整健康 = status=ok AND login_state=valid AND pagination_complete AND NOT rate_limited
  -- reached_timeline_end：本轮翻到时间线尽头（page>=maxPage），区别于「仅覆盖近窗/到 until」；
  --   短时间线账号据此直接判定基线已建立，避免自动回填反复请求越界页。
  -- parse_failure_count：本轮降级/无法解析的条目数；>0 视为不干净，基线判定不计入、留待重试。

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
  content_hash nullable, content_fidelity(full|preview|na),   -- content_hash 仅 full 必填；缺席/preview/na 降级时为空
  version_id nullable FK -> post_versions.id    -- 缺席、preview 或 na 降级时为空
  UNIQUE(fetch_run_id, post_id)

posts        (稳定身份登记 + 可变投影；禁删；身份字段不可改)
  id, author_id, platform, platform_post_id, first_seen_at,   -- 身份字段，锁定
  last_present_at, current_version_id FK, current_content_hash, current_image_manifest_hash,
  absent_healthy_streak(int default 0),
  feed_state, source_state, source_checked_at(nullable), watch_mode,
  posted_at_claimed, url, ingest_mode(live|backfill),
  raw_meta TEXT CHECK(raw_meta IS NULL OR json_valid(raw_meta))
  UNIQUE(platform, platform_post_id)

post_versions                    [append-only]
  id, post_id, content_text, content_hash, image_manifest_hash,
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
  label_reasoned_non_consensus(bool), is_market_related(bool),
  rationale, evidence_snippet, model, prompt_version, created_at
  UNIQUE(version_id, prompt_version)
  -- is_market_related 由归档正文中的明确 A 股代码或 raw_payload.stockCorrelation 派生；
  -- 旧库初始化时一次性回填，网页热查询只读该标志；已有 claim 也可进入市场相关观点页
  -- 后期加 attention_tier(0..3)，需 author 富化帖数 >= MIN_SAMPLES

post_images                     [append-only]
  id, version_id, source_url, normalized_url, ordinal,
  sha256(nullable), mime_type(nullable), byte_size(nullable), image_bytes(nullable),
  downloaded_at, download_status(ok|failed), notes
  -- 每次下载尝试追加一行；同 URL 字节变化通过新 sha256 + bytes_changed 留痕

image_ocr                       -- 派生转写，非原始证据
  id, image_id, image_sha256, engine, engine_version, ocr_text, created_at
  UNIQUE(image_id, engine, engine_version)

image_enrichments               -- VLM 推断，非原始证据
  id, image_id, image_sha256, model, prompt_version, prompt, description, created_at
  UNIQUE(image_id, model, prompt_version)

-- 阶段 4（占位）
claims
  id, post_id, version_id, author_id, ticker, direction(long|short|neutral),
  horizon_days(nullable), target_price(nullable), confidence_phrasing,
  claim_made_at(=对应版本 first_observed_at), ingest_mode(=对应版本),
  status(open|expired|resolved), created_at
claim_outcomes  claim_id, resolved_at, raw_return, benchmark_return, excess_return,
                outcome_method_version, notes
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

### 已完成阶段（详细规范与 DoD 见 `docs/phases-archive.md`，日常开发无需阅读）

- **阶段 1**（探针 + 存档 + 备份导出）：已完成。交付 `probe/`（探针脚本与 `probe_findings.md`）、
  `kol_archive/`（database/service/collector/models/browser）、`maintenance.py`（备份与脱敏导出）。
- **阶段 2**（时间线 + 证据卡片 + 关注理由 + 改写训练）：已完成。交付 `presentation.py`、
  `rewrite.py` 及对应 CLI 命令。
- **阶段 2b**（Vue 网页 + Tailscale 私网访问）：已完成。交付 `web.py`（serve 命令）+ `frontend/`。
- **阶段 3**（LLM 标签 + 标签门过滤 + 观点簇）：已完成。交付 `enrich.py`、队列/记分卡视图。
- **图片证据扩展**（下载/OCR/VLM，适配器 `xueqiu-3`）：已完成。交付 `images.py`、`ocr.py`、
  `image_enrich.py`。
- **阶段 5**（自我决策日志）：已完成。交付决策论点锁定、逐条结算、CLI 与网页录入/关闭/复盘闭环。
- **阶段 6**（中性变更摘要 + 主动通知基础）：已完成。交付 `digest.py`、`notifications.py`、
  `alerts.py`、`digest` 命令与 `run-once` 健康告警。
- **阶段 7**（lite 结算闭环）：已完成。交付 `claim_proposals`、命题提议与人工确认、
  `resolve-claims` 共同收盘结算及逐条结果展示。
- **阶段 8**（关注列表交集提醒）：已完成。交付关注列表管理、采集后交集提醒、防重流水和失败重试。
- 上述阶段全部 DoD 有自动化测试兜底（`tests/`，当前全套 240 通过）。

### 阶段 4：事件研究等（占位，按需）
行情对齐、交易日规则、基准选择、多时间窗口待定。可能冲突候选只摆证据；`claims` +
`claim_outcomes` 支持博主级战绩摘要、筛选和跨人排序。命中率、平均超额变化等指标必须使用版本化口径，
明确方向处理、基准、观察窗口、纳入与排除规则；页面同时展示已结算样本数、未结算数量、统计时间窗，
并允许下钻到全部组成命题。每条结算结果记录 `outcome_method_version`；默认排序使用
`PERFORMANCE_MIN_RESOLVED_SAMPLES` 作为最低已结算样本门槛，门槛以下只展示数据，不参与默认排名。
禁止隐藏负收益样本、把回填命题混入实时战绩、跨不同口径直接比较或生成不透明综合分。
事件研究 pandas 自写，导出只消费对应版本 `ingest_mode='live'` 且
`first_observed_at >= live_monitoring_started_at` 的命题，回填排除；回测库到时再评估。

**DoD**：同一口径可重算得到稳定指标；每个摘要和排名结果可追溯到组成命题与 `version_id`；
页面明确展示样本量、未结算数量、统计时间窗和口径版本；低于最低样本门槛的博主不进入默认排名；
回填命题、未结算命题和不可比口径不会污染战绩比较。

---

### 阶段 8：关注列表交集提醒

已完成。交付关注列表 CLI 与网页管理、采集后交集提醒、防重流水、失败重试和已结算记录中性标注；
详细规范见 `docs/phases-archive.md`。

### 阶段 9：统计分析层（删帖动机区分 + 拥挤度 + 防忽悠卡片）

全部为分布/描述层证据，宪章 12 约束适用：不对单次事件定性，无综合分。

- **选择性删除检验**：按博主比较「被删帖关联标的」与「保留帖关联标的」事后表现分布（共同口径、
  版本化方法、多窗口）；展示两组样本量；任一组低于 `analysis.min_group_samples`（默认 10）只显示
  「样本不足」。结果措辞为「分布差异」，不输出「该博主在改口」类结论。
- **跨博主拥挤度**：滚动窗口内 ≥ `analysis.crowding_min_authors`（默认 3）个账号对同一标的发布
  同向市场相关观点 → 记一条拥挤事件（append 流水表），事件页展示组成帖子与事后走势回看；
  不生成买卖暗示。
- **防忽悠卡片**：单帖证据卡片新增「该作者与本帖标的」面板——同作者同标的的全部历史版本（含已
  删除版本，照常标注观察语义）、历史观点簇的描述性市场变化、删除/编辑事件记录。无历史时诚实
  显示「无既往记录」。

**DoD**：两项统计同口径可重算；样本不足时无结论性文案（模板断言测试）；拥挤事件可下钻全部组成
帖；防忽悠面板包含已删版本且不暗示精确删改时刻；所有新增查询不拖慢单帖页首屏（建必要索引）。

### 阶段 10（可选）：框架提取 enrich-v3

- 对命中 `transferable_framework` 的版本，结构化抽取分析框架：输入变量、逻辑链、结论形态、
  作者声明的适用/失效条件；存独立派生表（幂等键含 `prompt_version`），不写回证据正文。
- 框架库页面：按主题/变量聚合浏览，逐条链回原帖版本。
- prompt 升级遵守现有 `enrich_prompt_version` 迁移机制。

**DoD**：抽取幂等可续跑；框架条目全部可溯源到 version_id；原文不可读时（已删）框架仍可用且
标注来源版本状态。

### 阶段 11：轻量多源信源层（Tier B 摄入）

把博主分两层：**Tier A（重装存证）只限雪球**，完整双轨监控、删帖检测、版本取证、结算闭环；
**Tier B（轻量信源）覆盖其他平台**，只做追加式摄入新帖——目标是「方向雷达」（发现值得自己
研究的方向），不是问责。Tier B 不打反爬攻防战，不承诺发现删帖或编辑。

- `authors` 加 `monitoring_tier('full'|'intake')`，现有雪球账号回填 `full`。schema 本就平台无关
  （`platform` 字段、适配器边界 6.7），下游复用不动状态机核心。
- **通用 RSS/JSON feed 适配器**（一个适配器吃多平台）：对接自部署 RSSHub 实例覆盖微博、B站、
  知乎、部分公众号等；Telegram 频道走官方 Bot API；普通博客直连 RSS。feed 条目映射
  `NormalizedPost`（`platform_post_id` 取 feed GUID/链接哈希，`content_fidelity` 按源标 full 或
  preview），原始条目存 `raw_payload`。RSSHub 地址、各源 URL 列表在 `config.local.yml`，
  凭据走环境变量。
- **手动入库入口**：对反爬极重的源（如公众号文章），CLI `import-post` + 网页粘贴表单，人工
  把正文/链接/作者/时间存为 intake 帖，同样进富化管线。
- 摄入帖照常写 `posts`/`post_versions`/`post_observations`（追加证据：留下「摄入当时的存档副本」
  本身有价值；两次拉取间内容变化照常落新版本，但**不承诺**捕获）。
- **Tier B 硬约束（与宪章同级）**：
  - 永不产生负面推断：无缺席 observation、无 `absent_confirmed`、无 streak、无 `out_of_scope`
    判定，`source_state` 不参与直链复查承诺（钉住 intake 帖只表达个人关注，不触发 Track B）。
  - 永不进入 `claim_proposals` / `claims` / 结算 / 任何战绩或排名；阶段 9 的选择性删除检验等
    统计一律排除 intake 数据（无删帖检测能力，统计无意义）。
  - UI 与导出明确标注层级（如「轻量信源 · 不监控删改」），不得与 Tier A 的监控语义混排造成
    「此帖受删帖监控」的错误暗示。
  - 噪音闸门：intake 帖进入待处理队列与过滤流仍须过现有标签门；原始时间线照常全量可达
    （防茧房原则不变）。watchlist 交集提醒（阶段 8）对 intake 帖生效——这是 Tier B 的主要
    变现出口。
- 依赖：晚于阶段 6（复用推送通道）与阶段 8（watchlist 出口）落地最划算，可与阶段 9/10 并行。

**DoD**：intake 账号任何轮次都不产生缺席 observation 与负面事件（有测试）；intake 版本不出现在
claim 提议目标、结算与阶段 9 统计中（有测试）；RSS 适配器用离线 fixture 直测（正常条目、缺字段
降级、GUID 去重）；手动入库可用且进富化管线；UI/导出层级标注可见；RSSHub 不可达只影响 intake
源采集、不连累雪球轮次。

### 明确不做（v9 冻结）

- 不做多平台**重装存证**（双轨监控、删帖检测、结算只限雪球）；多平台只走阶段 11 的轻量摄入层。
- 不做小样本排名；不做自动跟单、买卖信号或任何方向性建议输出。
- UI 视觉/品牌类迭代冻结：阶段 5–11 完成前不接受纯视觉任务，界面改动仅限承载上述功能所必需。

---

## 4. 给 Agent 的全局提醒
- 产出以证据和可复算指标帮助用户判断；观点摘要、博主比较和排序必须保留完整下钻路径。
- 过滤是默认视图但非唯一视图，原始时间线一键可达，防信息茧房。
- 不替用户写摘要让其只读摘要；原文始终可读。
- 阶段 1–2 跑通已是大部分价值；任意阶段可作合理终点。
- 全平台、全自动和精确归因成本高，非明确要求不主动扩张；博主级比较与排序属于照妖镜的核心实用能力，达到可比样本门槛后应优先完善。

---

## 5. 用户开工前填写的配置
- 探针选定平台；追踪账号与轮询频率初值。
- 监控窗口天数、缺席阈值 N、直链复查频率、`recent_feed_absent` 与 `llm_candidate` 的 TTL、`MIN_SAMPLES`、`PERFORMANCE_MIN_RESOLVED_SAMPLES`、各轨 `content_fidelity`（多数由探针建议）。
- `live_monitoring_started_at` 系统自动写入。
- LLM 供应商/模型/API Key（环境变量，阶段 2 起）。
- 行情数据源（阶段 4/7；日线已可由 `fetch-kline` 抓取）。
- 部署机器；阶段 2b 如需手机访问，配置 Tailscale 地址、端口和 ACL。
- 阶段 6+：推送通道及其凭据（环境变量）；`digest.wave_min_accounts`、`alerts.failure_streak`。
- 阶段 8：初始 watchlist（持仓与研究中标的）。
- 阶段 9：`analysis.min_group_samples`、`analysis.crowding_min_authors`。
- 阶段 11：自部署 RSSHub 地址、Tier B 信源清单（平台/账号/feed URL）、Telegram Bot Token
  （环境变量）、各源拉取频率。

---

## 6. 代码质量约束（Agent 执行守则，与第 0 节同级硬约束）

本节是可验收的工程纪律，不是风格偏好。凡涉及证据完整性、事务原子性、凭据安全的条目，违反即视为缺陷，优先级等同第 0 节宪章。

### 6.1 测试（核心，正确性靠测试兜底而非靠人读规范）

- **每条 DoD 对应至少一个自动化测试**；阶段不补齐对应测试，该阶段不算完成。测试用临时文件或 `:memory:` SQLite，连接同样 `PRAGMA foreign_keys=ON`。
- **必测的不变量**：① 六张证据表 `UPDATE/DELETE` 被触发器拒绝；② `posts` 删除被拒、身份字段 `id/author_id/platform/platform_post_id/first_seen_at` 改写被拒、其余字段可改；③ A→B→A 落三条版本行且首次观察时间各异；④ `content_fidelity=preview` 不建版本、不发 content 事件；⑤ feed 连续完整健康缺席达阈值 N 才 `absent_confirmed` 并入队，partial/failed run 不产生任何负面推断；⑥ `gone_confirmed` 黏性：其后 `restricted/not_found` 不降级，仅 `reachable` 翻回；⑦ 同一帖至多一条 `state='pending'`（部分唯一索引生效）；⑧ **解析失败降级路径**：旧帖原为 `absent_confirmed`，本轮识别出 `post_id` 但正文解析失败时，`fetch_run` 变 `partial`，写 `present=true`/`content_fidelity=na`/`version_id=null`/`content_hash=null` 的 observation，在场投影被重置（`feed_state=present`、`absent_healthy_streak=0`、`last_present_at` 更新），内容投影 `current_version_id/hash` 不变，且本轮对其他帖不执行任何负面推断（见 6.3）。
- **崩溃注入测试**：在「插证据 → 更新投影」之间强制抛异常，断言整条事务回滚、证据与投影不错位、无半写状态。
- **采集解析用离线 fixture**，不在测试里打真实平台；fixture 取自 `probe/raw/` 的真实响应样本，覆盖正常帖、置顶帖、删帖错误码（10022 登录失效 / 20210 not_found）、限权、分页边界。
- **「完整健康」判定**（feed: `status=ok AND login_state=valid AND pagination_complete AND NOT rate_limited`；直链: 去掉分页项）必须有独立单测覆盖各假值组合，这是负面推断与状态推进的总闸。

### 6.2 类型与静态检查

- 全量 type hints；`NormalizedPost` 等跨层契约用 `dataclass`/`TypedDict` 显式建模，不传裸 dict。
- 提交前过 `ruff`（lint+format）与类型检查器（mypy 或 pyright）零报错；CI 缺位时本地即视为门禁。
- 三个正交状态维度与各 `result/status` 枚举一律 `enum`，禁止裸字符串字面量散落。

### 6.3 错误处理与采集健壮性

- **失败必须被分类，绝不静默吞掉**：网络/超时/HTTP 错误 → 映射到 `fetch_run.status`、`login_state`、`probe_run.result` 等显式字段，宁可记 `unknown/partial/failed` 也不可把「采集失败」伪装成「确证缺席」（呼应宪章 6、7）。
- 退避温和：超时与重试有上限、带 jitter，遵守第 1 节「温和采集」；`rate_limited` 命中即如实置位并停止负面推断。
- 解析容错：单帖解析异常只让该帖降级，不连累整轮其他帖子已成功解析的正面存档。
- **解析失败必关负面推断（防误判缺席）**：**只要本轮任一帖子未能正常完成 `full` 或 `preview` 解析，就必须把 `fetch_run.status` 标为 `partial`**（绝不保留 `ok`）——触发条件只看「解析有没有正常完成」，与是否识别出 `post_id`、是否补写了降级 observation 无关（降级 na observation 不能洗白 partial）。否则 2.2 的健康门会把本轮实际已返回、只是解析失败的旧帖误计为缺席并累加 `absent_healthy_streak`。`partial` 经健康门即关闭本轮**全部**负面推断（缺席 observation、streak、`out_of_scope`），仅保留其他帖的正面存档。
  - **降级 observation（能否识别 `post_id` 只决定这一步，不影响上面的 partial 判定）**：识别出 `post_id` 时写一条——该帖确已在 feed 返回，故按在场处理：`present=true`、`content_fidelity=na`、`version_id=null`、`content_hash=null`（为此 `post_observations.content_hash` 须可空，仅 `full` 时必填）。它不是缺席，不参与负面推断。
  - **同步在场投影**：更新 `last_present_at`、`feed_state=present`、`absent_healthy_streak=0`，但**不动内容投影** `current_version_id/current_content_hash`（无可信正文）。否则已从 feed 返回的帖子会残留 `absent_confirmed`，证据与页面状态错位。

### 6.4 日志与自省

- 结构化日志，每个 `fetch_run/probe_run` 留可追溯痕迹（run id、覆盖范围、健康度、错误计数），对应阶段 1 的「自省日志」交付。
- **日志、异常消息、导出文件一律不得出现 cookie / API Key / 任何凭据**；记录请求时对敏感头做掩码。

### 6.5 凭据与安全

- API 密钥仅环境变量；登录 cookie 走环境变量或被 gitignore 的本地配置。**凭据绝不入库、绝不进导出、绝不进日志、绝不进异常文本、绝不提交**（呼应第 1 节与 1c）。
- 所有 SQL 用参数化绑定，禁止字符串拼接；面向用户/LLM 的文本入库前按数据列约束校验（JSON 列走 `json_valid`）。

### 6.6 数据库与事务纪律

- 一次采集的全部写入包进**单一事务**，崩溃整体回滚（宪章 2）；写入顺序按轨分别严格遵守第 2 节，不可混用：
  - **feed 侧**：`fetch_run → posts 空壳(新帖) → post_versions(full 变化) → post_observations → events → 更新 posts 投影`。`fetch_run` 不引用版本，最先插入；`post_observations.version_id` 引用版本，故版本行须在其之前。
  - **直链侧**：`(full 变化)post_version → probe_run(带 observed_version_id) → events → 更新 posts 投影`。仅此轨需"先版本行、后 run"，因 `probe_run.observed_version_id` 是版本外键。
- 每连接 `PRAGMA foreign_keys=ON`；WAL 模式；备份只用 backup API 或 `VACUUM INTO`，**禁止直接 cp 主文件**，且定期做恢复验证。
- 写一次即不可变的时间（`first_seen_at`、各 `first_observed_at`）在数据访问层也禁止重写，不止依赖触发器。

### 6.7 适配器边界与可演进性

- 平台耦合全部收敛在适配器内，对上只暴露 `NormalizedPost` 契约；状态机、存档逻辑不出现任何雪球字段名。
- **解析逻辑一变更，`adapter_version` 即递增**并写入对应 run，保证历史证据可溯源到当时的解析器。
- 解析尽量纯函数化（输入原始响应 → 输出 `NormalizedPost`，无副作用），便于用 fixture 直测。

### 6.8 时间处理

- 统一存储口径（UTC）；epoch 毫秒（如 `created_at/edited_at`）的转换集中在一处工具函数，禁止各处手写。
- 界面时间一律用「首次观察 / 最后观察 / 检测到缺失」语义（宪章 4），代码注释与变量命名不得暗示精确删改时刻。

### 6.9 简洁性（第一版刻意从简的延伸）

- 不预先引入容器、消息队列、ORM 框架、异步全家桶等第 1 节未列的依赖；新增第三方依赖需有明确必要性。
- 每阶段代码独立可跑、可作为合理终点（呼应宪章 11、第 4 节），不为后续阶段提前埋抽象。
