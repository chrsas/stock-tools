# 已完成阶段规范归档

> 本文件保存 `tasks.md` 中**已完成并有测试兜底**的阶段原始规范与 DoD，供追溯。
> 日常开发无需阅读本文件；宪章与质量约束以 `tasks.md` 为准，数据模型与状态机细节见
> `docs/architecture.md`。每完成一个阶段，将其详细规范从 `tasks.md` 移入此处，原位置只留
> 一行交付摘要。

---

### 阶段 1：平台探针 + 本地追加存档 + 备份导出

**1a 平台探针（先做，产出书面决策记录参数化后续）**
实测并记录：稳定持久帖子 ID；分页边界与 `covered_from/covered_to` 计算法；登录态维持与失效表现；编辑暴露方式；feed 中删帖表现；直链复查能否区分 `explicitly_removed / not_found / restricted / reachable`；直链正文能否稳定提取；**feed 与直链的正文能否归一化为相同内容**（不能则按轨标注 `content_fidelity`，feed 摘要型时编辑检测退化为仅 Track B 能力，须写明）；两轨各自频率限制；监控窗口默认天数；旧帖与钉住帖复查策略。**探针结论落地、参数回填后方可动 1b。**

**1b 存档与双轨状态机**
- 单进程 + SQLite(WAL)，连接级 `PRAGMA foreign_keys=ON`；建全部表、视图、UNIQUE/部分唯一索引、FK。
- 触发器：六张证据表只追加；`posts` 禁删与身份字段不可改。
- feed 与直链两任务，各按 `docs/architecture.md` 的事务顺序原子写入；实现该文件 Track A/B、内容版本处理、末次观察推导、钉住与队列全部规则。

**1c 备份与导出**
SQLite backup API 或 `VACUUM INTO` 定时多份快照，定期恢复验证；JSON/CSV 导出；API 密钥仅环境变量，登录 cookie 可用环境变量或被忽略的本地配置，任何凭据绝不进导出文件。

**DoD**：A→B→A 落三条版本行且各带不同首次观察时间；partial run 中已见在场帖仍存档、但不产生缺席推断；full/preview 正确区分，preview 不触发编辑事件；feed 连续完整健康缺席落 `absent_confirmed` 并入队；健康直链删除占位落 `gone_confirmed` 且后续 404/限权不降级，正文被改可捕获为新版本；退化抓取/复查不动帖子状态与 `source_checked_at`；同一帖至多一条 pending；崩溃注入下事务整体回滚、证据与投影不错位；`posts` 身份字段与删除被触发器拒绝；备份可恢复且经验证。

---

### 阶段 2：原始时间线 + 证据卡片 + 关注理由 + 改写训练
不建批量富化。时间线（三维状态人读标签、删帖强弱信号分级）；证据卡片（无 LLM，单帖观察历史、版本 diff、变迁及证据 run、附注、钉住开关）；`attention_log`（锁 `version_id`、创建即自动钉住）；改写训练（按需单条 LLM 改写写 `rewrite_exercises` 含 `version_id`、创建即自动钉住）。
**DoD**：原始流与诚实观察时间可见；钉/取消钉遵守 `docs/architecture.md` 的钉住与队列规则；写理由或改写训练自动钉住并锁版本。

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

### 阶段 5：自我决策日志

目标函数从「监督 KOL」扩展到「监督自己」：用同一套不可篡改存证与价格结算机器记录用户本人的
投资决策并强制复盘，独立于 KOL 数据可用。

```
my_decisions          (身份与论点锁定，状态可变投影)
  id, ticker, direction(long|short|neutral), thesis_text,
  invalidation_condition, horizon_days(nullable), position_note(nullable),
  decided_at, source_post_id(nullable FK), source_version_id(nullable FK),
  status(open|invalidated|expired|closed), closed_at(nullable), notes
my_decision_outcomes
  id, decision_id, resolved_at, raw_return, benchmark_return, excess_return,
  outcome_method_version, notes
my_decision_reviews              [append-only]
  id, decision_id, reviewed_at, retro_text, lesson(nullable)
```

- 数据库触发器锁定决策的 ticker、方向、原始论点、证伪条件、决策时间与来源关联，禁止物理删除；
  状态、关闭时间和备注可更新，复盘记录只追加。
- CLI 提供 `add-decision`、`close-decision`、`review-decision`、`decisions` 和
  `resolve-decisions`。列表展示到期未结算、逾期未复盘与逐条结果。
- 网页“我的决策”视图提供录入、按状态与标的筛选、人工关闭、追加复盘和逐条结果下钻，写操作
  全部使用 POST 与 CSRF。
- `invalidation_condition` 必填。证伪是否触发由人工判定，工具只展示走势证据与到期提醒。
- 结算复用 `prices` 共同交易日收盘口径，首版方法为 `descriptive-common-close-v1`：决策发生日前
  最后一个共同交易日收盘为起点，期限自然日当天或之后首个共同交易日收盘为终点；缺少任一端行情
  时保持待结算。结果记录基准代码，按决策、基准和方法版本幂等落库，写入后不可修改或删除；冲突
  重算明确报错，逐条展示且不汇总打分。
- 决策、结果和复盘纳入脱敏导出。

**DoD**：论点字段 UPDATE 被触发器拒绝、status 可改、行禁删；无证伪条件录入被拒；结算同口径
可重算稳定；到期未结算与未复盘清单在 CLI 与网页可见；决策、结果、复盘逐条可下钻；全流程不
输出任何方向性建议文案。

### 阶段 6：中性变更摘要 + 主动通知基础

把工具从「等用户来查」变成「主动告知」，同时为后续阶段提供推送通道。措辞受宪章 7/12 约束。

- `digest` 命令：给定窗口（默认 7 天）生成 markdown/HTML 摘要，含：
  - 删除事件：帖子标识、存档正文摘录与图片缩略、首次/最后观察时间（不归因动机）；
  - 编辑事件：版本 diff 高亮；
  - 仅图片变更（正文未动、`image_manifest_hash` 变化）单独列出；
  - 跨账号同步删帖潮：窗口内达到 `digest.wave_min_accounts`（默认 3）个账号发生删除事件即
    整体标注「平台级删帖密集期」，逐条仍列出但不对个人加注；
  - 涉及观点簇的，附已有的描述性市场变化。
- 通知通道：摘要落盘 `data/digests/`；可选推送（通道与凭据走环境变量/本地配置）。推送内容只含
  摘要标题、条目计数和 tailnet 私网链接，证据正文与截图不出私网；第三方推送服务只承载该
  最小载荷。
- 采集健康告警：`run-once` 连续失败、CDP 连不上、`login_state=expired` 连续出现达到
  `alerts.failure_streak`（默认 2）即通过同通道告警。

**DoD**：摘要措辞无归因词（对模板做断言测试）；删帖潮判定有单测；推送载荷不含正文、图片或
凭据；采集连续失败可在阈值内触发告警；窗口内无事件时输出「无变更」而非空文件。

### 阶段 7：lite 结算闭环

跑通「LLM 提议 → 人工确认 → 自动结算 → 逐条展示」。宪章 9/10/11 全部适用。

```
claim_proposals
  id, version_id, ticker, direction(long|short|neutral),
  horizon_days(nullable), target_price(nullable), confidence_phrasing,
  evidence_snippet, model, prompt_version, created_at,
  review_state(pending|accepted|rejected), reviewed_at(nullable),
  claim_id(nullable FK -> claims.id)
  UNIQUE(version_id, ticker, prompt_version)
```

- `propose-claims` 仅对实时监控起点后的市场相关 live 版本运行；LLM 只能抽取原文明示字段；
  `claim_proposal_scans` 记录含零提议在内的扫描结果，避免重复调用。
- 网页确认页和 CLI 支持 accepted/rejected；accepted 与写入 `claims` 同事务，rejected 留痕。
- `resolve-claims` 到期用共同交易日收盘写入不可变 `claim_outcomes`，记录基准代码和口径版本。
- 博主观点页逐条展示结果，原帖不可见时明确指向归档版本；不生成小样本排名或命中率汇总。

**DoD**：提议幂等可重跑；未确认提议不进 claims；监控起点前版本被排除；LLM 补造字段被拒收；
接受与 claims 写入原子提交；结算结果不可变且可重算稳定；逐条结果可下钻到版本证据。

### 阶段 8：关注列表交集提醒

- `watchlist` 保存标的代码、可选名称、加入时间和备注；CLI 提供 `watch-ticker`、
  `unwatch-ticker`、`watchlist`，网页提供关注列表增删入口。
- `watchlist_alerts` 以 `(version_id, ticker)` 唯一约束防重，保留检测时间与成功发送时间。
- `run-once` 成功完成采集后，独立扫描本轮新增 live 版本与关注列表的交集。通知只携带作者名、
  标的代码和私网帖子链接，正文不离开私网。
- 通知失败时保留未发送流水，后续运行继续重试；通知未启用或缺少凭据时跳过扫描，不生成流水；
  发送过程在采集事务之后执行，错误只记录日志。
- 同作者同标的已有不可变 `claim_outcomes` 时，通知仅标注「该作者此标的有已结算记录」，不附结论。
- `watchlist` 与提醒流水纳入凭据安全导出。

**DoD**：交集命中即提醒；重复运行不重复提醒；正文不出私网；关注列表增删有 CLI 与网页入口；
推送失败不影响采集结果并可在后续运行重试。

### 阶段 9：统计分析层

- `version_tickers` 将归档版本中的明确 A 股代码持久化为派生索引。旧库初始化时回填，新版本与采集
  事务同步写入，单帖页不使用 `json_tree` 或正文通配扫描。
- 选择性删除检验按作者、命题期限、基准和结算口径分组，只比较已结算 live 命题，并仅将在命题
  形成后至结算时观察到明确移除的帖子计入移除组。页面始终展示
  两组样本量；任一组低于 `analysis.min_group_samples` 时只显示“样本不足”，不展示分布指标。
- `crowding_events` 与 `crowding_event_members` 是只追加派生流水。`analyze` 命令按已确认命题的
  同标的、同方向和滚动窗口生成事件，达到独立作者门槛才落库，重叠窗口归并为一次事件，事件可
  下钻全部组成帖子与版本。
- 单帖“该作者与本帖标的”面板通过 `version_tickers` 索引列出同作者同标的全部历史版本，包含
  已明确移除版本、删除或编辑事件和已有描述性市场变化；无明确标的时显示“无既往记录”。
- 新增表纳入凭据安全导出；拥挤事件与成员同事务写入，注入失败时整体回滚。

**DoD**：选择性删除分析同期限同口径可重算，样本不足无结论性文案；拥挤事件只追加且可下钻全部
组成证据；单帖历史面板包含已移除版本，不暗示精确删改时刻；单帖查询使用持久化索引。

### 阶段 10：框架提取 enrich-v3

已完成。交付 `framework_extractions` 与 `framework_extraction_scans` 派生表，幂等键包含
`prompt_version`，零结果扫描留痕不重试。新增 `extract-frameworks` CLI 与框架库网页
`?view=frameworks`，按主题和输入变量聚合，逐条链回 `version_id`。

原帖不可读时，框架仍可用，并以中性措辞标注来源状态。prompt 版本走
`llm.framework_prompt_version`，默认 `framework-v1`；升级后旧行保留，新版本重扫。

**DoD**：框架提取按 `version_id + prompt_version` 幂等；零结果扫描不会反复调用 LLM；框架库可按
主题和变量聚合并下钻到版本证据；原帖不可读时文案保持中性；prompt 升级不覆盖旧结果。
