# KOL 发言追踪工具任务说明

本文件是日常开发入口。开工前读第 0 节、第 6 节和当前要做的阶段小节。详细架构见 `docs/architecture.md`，已完成阶段细则见 `docs/phases-archive.md`。

## 0. 项目宪章

工具定位是「照妖镜 + 注意力分配器」：用可追溯证据帮助用户判断谁的公开观点经得起后续市场检验、谁反复改口，并把高价值内容捞出来。可提供博主级战绩摘要、比较和排序，用于决定关注顺序；禁止包装成收益承诺、自动跟单信号或投资建议。

1. 证据只追加。`fetch_runs / probe_runs / post_observations / post_versions / post_events / post_images` 禁止修改和删除。`posts` 禁止物理删除，身份字段 `id / author_id / platform / platform_post_id / first_seen_at` 不可改，其余字段是可由证据重算的投影。
2. 一次采集的写入必须在同一 SQLite 事务内原子提交。feed 顺序是 `fetch_run -> posts -> post_versions -> post_observations -> events -> posts 投影`；直链顺序是 `post_versions -> probe_run -> events -> posts 投影`。
3. `first_seen_at` 与各 `first_observed_at` 写一次即不可变。末次观察时间从 `version_sightings` 视图聚合。
4. 只能保存系统实际观察到的版本。界面使用「首次观察」「最后观察」「检测到缺失」，不得暗示精确删改时刻。
5. 状态分为 `feed_state`、`source_state`、`watch_mode` 三个正交维度。`edited` 只作为历史事件。
6. 健康门只限制负面推断。成功解析或轻解析出的在场帖子参与本轮 seen set；首页指纹未变时每天最多完整观察首页一次；正向 observation 只在新帖、内容或图片变化、解析降级、状态恢复，或稳定旧帖达到观察间隔且未达到观察次数上限时追加。只有完整健康 run 才能生成缺席 observation、累计 streak、判定 `out_of_scope`。
7. 证据层不判断删帖主体。`gone_confirmed` 只表示来源页明确显示已移除；裸 404、跳转、限权记为 `unavailable`。
8. 未钉住且滑出监控窗口的帖子停止持续监控。存档保留，但不承诺继续发现删除或编辑。
9. 历史回填与实时监控按版本隔离。只有 `ingest_mode='live'` 且 `first_observed_at >= live_monitoring_started_at` 的版本对应命题可进入事件研究。
10. `claims` 只收原帖明确表达过的可证伪内容，`claim_made_at` 取对应版本首次观察时间。LLM 改写只进 `rewrite_exercises`，不得流入回测；`horizon_days`、`target_price` 允许为空，LLM 不得补造。
11. LLM 只做富化，不参与判定市场结果。博主级摘要、筛选和排序必须展示样本量、统计时间窗、口径版本、未结算数量，并能下钻到 `version_id`、命题和市场结果。禁止隐藏亏损样本、混用不可比口径、用小样本强行排名或生成不透明综合分。
12. 呈现层对删帖动机保持中性。摘要、周报、UI、通知使用「删除」「编辑」「图片变更」等事实表述，禁止「改口」「心虚」等归因措辞。分布层统计必须展示样本量，样本不足只显示「不足」。
13. 工具本地单用户使用，不对外发布，不暴露公网，采集保持温和。

## 1. 当前状态

已完成阶段的详细规范和 DoD 在 `docs/phases-archive.md`。当前测试基线：pytest 359 通过，前端测试 8 通过。

| 阶段 | 状态 | 交付摘要 |
|---|---|---|
| 1 | 已完成 | 平台探针、本地追加存档、备份导出 |
| 2 | 已完成 | 原始时间线、证据卡片、关注理由、改写训练 |
| 2b | 已完成 | Vue 网页、Python `serve` 命令、Tailscale 私网访问 |
| 3 | 已完成 | LLM 标签、标签门过滤、观点簇 |
| 图片证据扩展 | 已完成 | 图片下载、OCR、VLM 富化 |
| 5 | 已完成 | 自我决策日志、逐条结算、复盘闭环 |
| 6 | 已完成 | 中性变更摘要、主动通知、采集健康告警 |
| 7 | 已完成 | lite 结算闭环、命题提议、人工确认、到期结算 |
| 8 | 已完成 | 关注列表管理、交集提醒、防重流水、失败重试 |
| 9 | 已完成 | 选择性删除分布、跨博主拥挤流水、单帖同标的历史面板 |
| 10 | 已完成 | 框架提取 enrich-v3、框架库页面、版本化 prompt |

## 2. 当前阶段

### 阶段 11：轻量多源信源层

目标：保留雪球作为 Tier A 重装存证源，新增其他平台的 Tier B 轻量摄入源。Tier B 只做追加式新帖摄入和富化，用于发现研究方向；不承诺发现删帖、编辑或持续复查。

核心范围：

1. `authors` 增加 `monitoring_tier('full'|'intake')`，现有雪球账号回填 `full`。
2. 新增通用 RSS/JSON feed 适配器，对接自部署 RSSHub、Telegram Bot API、普通博客 RSS。条目映射 `NormalizedPost`，`platform_post_id` 取 GUID 或链接哈希，`content_fidelity` 按来源标记。
3. 新增手动入库入口，CLI `import-post` 和网页粘贴表单均按 intake 帖进入富化管线。
4. intake 帖可以写 `posts / post_versions / post_observations` 作为摄入时证据；两次拉取间内容变化可以落新版本，但不承诺完整捕获。
5. UI 与导出必须明确标注「轻量信源，不监控删改」，避免和 Tier A 监控语义混排。
6. watchlist 交集提醒对 intake 帖生效，这是 Tier B 的主要出口。

硬约束：

1. intake 账号永不产生缺席 observation、负面事件、`absent_confirmed`、streak、`out_of_scope`。
2. intake 帖不进入 `claim_proposals / claims / 结算 / 战绩 / 排名`。
3. 阶段 9 的选择性删除检验等统计排除 intake 数据。
4. RSSHub 或某个 intake 源不可达时，只影响对应 intake 源采集，不连累雪球轮次。

DoD：

1. intake 账号任意轮次都不产生缺席 observation 与负面事件，自动化测试覆盖。
2. intake 版本不出现在 claim 提议、结算、阶段 9 统计中，自动化测试覆盖。
3. RSS 适配器用离线 fixture 覆盖正常条目、缺字段降级、GUID 去重。
4. 手动入库可用，且进入富化管线。
5. UI 与导出层级标注可见。

## 3. 延后事项

### 阶段 4：事件研究

行情对齐、交易日规则、基准选择、多时间窗口待定。`claims + claim_outcomes` 已支持博主级战绩摘要、筛选和跨人排序。后续指标必须使用版本化口径，明确方向处理、基准、观察窗口、纳入与排除规则；页面同时展示已结算样本数、未结算数量、统计时间窗，并允许下钻到全部组成命题。

DoD：同一口径可重算得到稳定指标；每个摘要和排名结果可追溯到组成命题与 `version_id`；低于最低样本门槛的博主不进入默认排名；回填命题、未结算命题和不可比口径不会污染战绩比较。

### 明确不做

1. 多平台重装存证。双轨监控、删帖检测、结算只限雪球；多平台只走阶段 11 的轻量摄入层。
2. 小样本排名、自动跟单、买卖信号或任何方向性建议输出。
3. 阶段 5 到 11 完成前冻结纯视觉任务，界面改动限于承载功能所需。

## 4. 运行配置入口

运行配置在 `config/config.yml`、本地未提交配置和环境变量中维护。关键项包括追踪账号与博主级调度、请求预算、监控窗口天数、缺席阈值 N、直链复查频率、TTL、样本门槛、LLM 供应商和模型、行情数据源、推送通道、watchlist、分析阈值、`llm.framework_prompt_version`、RSSHub 地址、Tier B 信源清单和 Telegram Bot Token。

凭据只允许放环境变量或被 gitignore 的本地配置；不得入库、入导出、入日志、入异常文本或进入提交。

## 5. 给 Agent 的提醒

1. 产出要保留证据和可复算指标，摘要、比较和排序都必须可下钻。
2. 过滤是默认视图，原始时间线必须一键可达。
3. 原文始终可读，摘要不能替代原始证据。
4. 不主动扩张到全平台、全自动或精确归因。博主级比较与排序达到可比样本门槛后优先完善。
5. 每阶段代码独立可跑、可作为合理终点，不为后续阶段提前埋抽象。
6. 技术栈、数据模型、状态机细节见 `docs/architecture.md`。

## 6. 代码质量约束

本节与第 0 节同级，违反证据完整性、事务原子性、凭据安全的条目视为缺陷。

1. 每条 DoD 至少对应一个自动化测试。测试用临时文件或 `:memory:` SQLite，并开启 `PRAGMA foreign_keys=ON`。
2. 必测不变量：证据表不可改删；`posts` 禁删且身份字段不可改；A 到 B 到 A 版本链保留不同首次观察时间；preview 不建版本、不发 content 事件；完整健康 feed 连续缺席达阈值才产生 `absent_confirmed`；partial/failed run 不产生负面推断；`gone_confirmed` 只可由 reachable 翻回；同一帖至多一条 pending 队列项。
3. 解析失败必须关闭本轮全部负面推断。任一帖子未完成 full 或 preview 解析，`fetch_run.status` 必须是 `partial`。若识别出 `post_id`，写 `present=true / content_fidelity=na / version_id=null / content_hash=null`，并重置在场投影，不改内容投影。
4. 崩溃注入测试覆盖「插证据后、更新投影前」异常，断言事务整体回滚。
5. 采集解析测试使用离线 fixture，不在测试里打真实平台。
6. 跨层契约使用 `dataclass` 或 `TypedDict`；状态、result、status 使用 enum；提交前 `ruff`、格式检查、类型检查必须通过。
7. 失败必须分类并落到显式字段，禁止把采集失败伪装成确证缺席。超时与重试有上限并带 jitter，`rate_limited` 命中后停止负面推断。
8. 日志记录 run id、覆盖范围、健康度、错误计数。日志、异常、导出不得出现 cookie、API Key 或任何凭据。
9. SQL 使用参数化绑定。JSON 列写入前按列约束校验。
10. 每连接开启外键；WAL 模式；备份只用 SQLite backup API 或 `VACUUM INTO`，并定期做恢复验证。
11. 平台耦合收敛在适配器内，对上只暴露 `NormalizedPost`；解析逻辑变更必须递增 `adapter_version`。
12. 时间统一按 UTC 存储。界面、注释和变量命名不得暗示精确删改时刻。
13. 不预先引入容器、消息队列、ORM、异步全家桶等依赖；新增第三方依赖必须有明确必要性。

提交前必须运行 `.\scripts\check_quality.ps1`，并检查 `git status` 与 diff，确认凭据、本地配置、运行数据和原始采集文件未进入提交。
