# KOL 发言追踪工具 开发规范 v8（冻结候选，供 Agent 执行）

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

### 阶段 1：平台探针 + 本地追加存档 + 备份导出

**1a 平台探针（先做，产出书面决策记录参数化后续）**
实测并记录：稳定持久帖子 ID；分页边界与 `covered_from/covered_to` 计算法；登录态维持与失效表现；编辑暴露方式；feed 中删帖表现；直链复查能否区分 `explicitly_removed / not_found / restricted / reachable`；直链正文能否稳定提取；**feed 与直链的正文能否归一化为相同内容**（不能则按轨标注 `content_fidelity`，feed 摘要型时编辑检测退化为仅 Track B 能力，须写明）；两轨各自频率限制；监控窗口默认天数；旧帖与钉住帖复查策略。**探针结论落地、参数回填后方可动 1b。**

**1b 存档与双轨状态机**
- 单进程 + SQLite(WAL)，连接级 `PRAGMA foreign_keys=ON`；建全部表、视图、UNIQUE/部分唯一索引、FK。
- 触发器：六张证据表只追加；`posts` 禁删与身份字段不可改。
- feed 与直链两任务，各按第 2 节事务顺序原子写入；实现 2.2–2.6 全部规则。

**1c 备份与导出**
SQLite backup API 或 `VACUUM INTO` 定时多份快照，定期恢复验证；JSON/CSV 导出；API 密钥仅环境变量，登录 cookie 可用环境变量或被忽略的本地配置，任何凭据绝不进导出文件。

**DoD**：A→B→A 落三条版本行且各带不同首次观察时间；partial run 中已见在场帖仍存档、但不产生缺席推断；full/preview 正确区分，preview 不触发编辑事件；feed 连续完整健康缺席落 `absent_confirmed` 并入队；健康直链删除占位落 `gone_confirmed` 且后续 404/限权不降级，正文被改可捕获为新版本；退化抓取/复查不动帖子状态与 `source_checked_at`；同一帖至多一条 pending；崩溃注入下事务整体回滚、证据与投影不错位；`posts` 身份字段与删除被触发器拒绝；备份可恢复且经验证。

---

### 阶段 2：原始时间线 + 证据卡片 + 关注理由 + 改写训练
不建批量富化。时间线（三维状态人读标签、删帖强弱信号分级）；证据卡片（无 LLM，单帖观察历史、版本 diff、变迁及证据 run、附注、钉住开关）；`attention_log`（锁 `version_id`、创建即自动钉住）；改写训练（按需单条 LLM 改写写 `rewrite_exercises` 含 `version_id`、创建即自动钉住）。
**DoD**：原始流与诚实观察时间可见；钉/取消钉遵守 2.6；写理由或改写训练自动钉住并锁版本。

### 阶段 2b：轻量网页界面 + 手机私网访问
前端源码放在 `frontend/`，构建产物由现有 `serve` 命令托管。网页与 CLI 共用现有 SQLite、
状态机、展示投影和脱敏逻辑，继续保持单 Python 进程。

**2b1 本机网页闭环**
- 新增 `serve --config-dir config` 启动命令，默认只监听 `127.0.0.1`。
- 默认首页展示博主最近市场相关观点；左侧选择博主并展示已结算样本数、未结算数量和可用战绩摘要，右侧展示最近观点簇和已记录市场结果。尚未配置正式结算口径时，只展示已有记录及其原始口径。
- 已有可用结算口径时，博主列表支持按最近活跃、已结算样本数、命中率、平均超额变化等明确指标排序。低于 `PERFORMANCE_MIN_RESOLVED_SAMPLES` 时默认按最近活跃展示并标注“样本不足”，不得用零样本或小样本制造领先印象。
- 服务端渲染原始时间线和证据卡片，原文始终可读；手机窄屏下保持可用。
- 时间线展示三维状态、人读标签、删帖强弱信号、首次观察、最后观察和检测到缺失时间。
- 证据卡片展示观察历史、版本列表、版本 diff、状态变迁、关联 run、脱敏附注、关注理由、改写训练和钉住状态。
- 页面提供钉住、取消钉住、写关注理由、单条改写训练和记录人工 verdict 的操作入口；写入继续复用阶段 2 服务层。

**2b2 手机私网访问**
- 需要手机访问时，在部署机器和手机安装 Tailscale，只通过 tailnet 私网地址访问。
- 服务监听地址必须显式配置为部署机器的 Tailscale 地址；默认值仍为 `127.0.0.1`，不得默认监听 `0.0.0.0`。
- 不配置公网端口映射，不开放公网访问，不在页面、日志或异常中输出 cookie、API Key 或本地配置内容。
- 所有修改状态的网页请求使用 `POST`，并校验 CSRF token；Tailscale ACL 仅允许用户自己的设备访问。

**DoD**：本机浏览器可以完成阶段 2 的查看与操作闭环；已有可用结算口径时，博主摘要与排序同时展示样本量、时间窗和口径说明，且可下钻到对应观点证据；样本不足不会进入默认战绩排序；页面只展示脱敏后的证据；CLI 行为保持兼容；默认监听仅限本机；显式绑定 Tailscale 地址后手机可在 tailnet 内访问；公网无法访问；网页路由、脱敏、CSRF 和关键写操作有自动化测试。

### 阶段 3：LLM 标签 + 标签门过滤（需累积样本）
批量富化每个 `version` 出 `post_type`、三布尔标签、`rationale`、`evidence_snippet`、`model`、
`prompt_version`（幂等键 UNIQUE(version_id, prompt_version)）；归档证据另行派生
`is_market_related`，用于博主观点页减噪。命中任一标签进过滤流按时间排序，原始流一键可达；
市场相关观点按博主展示，同一明确证券代码在连续 7 天窗口内聚合为观点簇；后期达
`MIN_SAMPLES` 加 `attention_tier`，按 `prompt_version` 隔离。`attention_tier` 只表达内容关注价值，
不得替代基于已结算市场结果计算的战绩指标。
**DoD**：命中标签进过滤流；市场无关观点不进入博主观点页但仍保留在原始流和富化记录；
观点簇逐条保留原帖证据；原始流始终可达；样本不足不强行分级。

### 图片证据扩展：下载 + OCR + VLM
帖子版本记录去签名后的有序图片清单哈希，正文不变但换图、增图或删图也生成新版本。
图片字节按下载尝试追加到 `post_images`；OCR 与 VLM 描述分别进入派生表，不写回证据正文。
三个 pass 独立按需运行，单图失败不阻断批次；导出保留图片字节和 OCR 原文，对图片来源 URL
去查询参数，并对 VLM 描述执行启发式脱敏。

**DoD**：图片清单变化可形成新版本；同 URL 字节变化可追溯；下载、OCR、VLM 均可续跑且幂等；
旧版本 NULL 清单升级后首轮不误报换图；导出不携带图片签名参数。

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
- 行情数据源（阶段 4）。
- 部署机器；阶段 2b 如需手机访问，配置 Tailscale 地址、端口和 ACL。

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
