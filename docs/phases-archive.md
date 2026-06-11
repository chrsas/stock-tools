# 已完成阶段规范归档

> 本文件保存 `tasks.md` 中**已完成并有测试兜底**的阶段原始规范与 DoD，供追溯。
> 日常开发无需阅读本文件；宪章（第 0 节）、数据模型（第 2 节）与质量约束（第 6 节）
> 仍以 `tasks.md` 为准。每完成一个阶段，将其详细规范从 `tasks.md` 移入此处，
> 原位置只留一行交付摘要。

---

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
