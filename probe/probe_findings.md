# 阶段 1a 平台探针决策记录 — 雪球（xueqiu.com）

> 状态：**结论落地**。本文件回答 tasks.md 阶段 1a 探针清单全部 10 项，并回填第 5 节配置参数。
> 所有结论均有 `probe/raw/` 下的原始响应为证。探针脚本：`probe/probe_xueqiu.py`、`probe/probe_more.py`。
> 探测样本账号 uid=`7377966687`，样本帖 id=`391984427`（公开热门流随机取得，非追踪目标）。
> 采集方式：只读、单会话、每请求间隔 1.5–2s、每会话仅 1 次首页引导。全程 ~35 请求**零限频**。

---

## 0. 一句话结论

雪球可用**匿名 guest token**完成第一版大部分采集：feed 第 1 页、公开帖直链、HTML 可达性判断全部可读；
**翻页与回填需要登录 cookie**。两轨正文以 `text` 字段为正典，经统一归一化后 **feed 与直链产出同一 `content_hash`**，跨轨编辑检测成立，两轨均 `content_fidelity=full`（普通帖）。

---

## 1. 稳定持久帖子 ID（清单 1）

- `platform_post_id` = 状态对象的数字 `id`（样本 `391984427`）。跨 feed / 直链 / HTML 三处一致。
- 作者 `platform_uid` = `user_id`（数字，样本 `7377966687`）。
- 规范 `posts.url` = `https://xueqiu.com/{user_id}/{id}`；接口直接给出 `target` = `"/{user_id}/{id}"`，拼接即可。
- `posted_at_claimed` 来源 = `created_at`（**epoch 毫秒**）。

> 证据：`raw/01_discover_listV2.json.json`、`raw/02_timeline_*.json`、`raw/03_show_391984427.json`。

---

## 2. 分页边界与 covered_from / covered_to（清单 2）

- 接口：`GET https://xueqiu.com/v4/statuses/user_timeline.json?user_id={uid}&page={n}`
- 返回 `{count, statuses[], total, page, maxPage, banner}`；每页 ~20 条 + 可能 1 条置顶。
- **置顶帖陷阱**：置顶帖以 `mark=1` 标记，被顶到数组 **index 0**，其 `created_at` 不在降序位置（样本：置顶为 2025-11-07，其余为 2026-04~05）。index≥1 的其余帖**严格按 `created_at` 降序**。
- **covered_from/to 算法**：
  1. 剔除 `mark==1` 的置顶帖；
  2. `covered_to` = 本轮抓到的非置顶帖 `max(created_at)`；
  3. `covered_from` = 已连续抓取页的非置顶帖 `min(created_at)`；
  4. `pagination_complete` = （抓到的最旧帖 `created_at` ≤ 监控窗口起点）或（`page == maxPage`）。
- **guest 只能读 page 1**（见第 3 节），故 guest 模式下 `pagination_complete=true` 仅当监控窗口完全落在 page 1 的日期跨度内。
- 采样账号 page1（20 非置顶帖）跨 **~38 天**（中位间隔 0.9 天/帖，total 1821、maxPage 92）→ **30 天窗口对该类账号 guest 即够**。

> 证据：`raw/02_timeline_7377966687_p1.json`（page/maxPage/total）、`raw/02_timeline_7377966687_p2.json`（page2 报错）。

---

## 3. 登录态维持与失效表现（清单 3）

- **guest cookie**：`GET https://xueqiu.com/` 即下发 `xq_a_token / xqat / xq_r_token / xq_id_token / u / cookiesu`，无需登录。
  - 足够：feed page 1、公开帖 `show.json`、HTML 页状态码。
  - 不足：feed **page≥2**、关注可见/私密内容、完整回填。
- **失效/降级信号**：
  - feed page≥2 → HTTP **400 + `error_code=10022`「请登录雪球查看更多内容」**。
  - 登录 cookie 过期同样表现为 `10022` / 400。
- **映射到证据表**：见到 `10022` → `fetch_runs.login_state=expired`（Track A）、`probe_runs` **健康门不通过**（Track B），按规范 §2.3 退化复查只留痕迹、不改帖子状态与 `source_checked_at`。
- **配置建议**：真实运营应让用户从已登录浏览器导出 cookie（至少 `xq_a_token`、`xqat`、`u`），仅存环境变量/本地 config.local，**绝不入库、绝不进导出**（规范硬约束）。会话级先 guest 引导拿设备 cookie（`aliyungf_tc` 等），再覆盖用户的 `xq_a_token/xqat/u`。
- **带登录补测已验证（2026-06-01）**：注入真实登录 cookie 后，同一账号 `page=2` 返回 **200**（maxPage=246），`10022` 限制解除——**登录态确实打通多页与回填**。cookie 注入方式（guest 引导 + 覆盖 xq_* ）有效。脚本 `probe/probe_login.py`。

> 证据：`raw/00_bootstrap_cookies.json`、`raw/02_timeline_7377966687_p2.json`。

---

## 4. 编辑暴露方式（清单 4）

- 状态对象含 **`edited_at`**（epoch 毫秒，未编辑为 `null`）。样本中实测到一条真实被编辑帖（转发原帖 `edited_at=1762341936000`，且 `is_column=true`），证实发生编辑时该字段回填。
- `editable` / `canEdit` 是「**当前观察者**能否编辑」，**非**编辑历史，**不可**用作编辑信号。
- **采用策略**：仍按规范 §2.4 以 `content_hash` 变化为编辑判定主依据（追加新 `post_version`）；`edited_at` 作为**佐证**写入 `raw_payload`，可在证据卡片展示，但界面表述遵守 §0.4「首次/最后观察、检测到缺失」，不暗示精确修改时刻。

> 证据：`raw/02_timeline_7377966687_p1.json`（含 `edited_at` 非空的转发原帖）。

---

## 5. feed 中删帖表现（清单 5）

- 删帖**不会**在 feed 里留下墓碑——已删帖直接从 `user_timeline` 消失。
- 因此 feed 侧删除只能走规范 §2.2 的**负面推断**：完整健康 run 中、覆盖范围内未再出现 → `present=false` observation → `absent_healthy_streak` 累计 → 达阈值 N 入 `recheck_queue(recent_feed_absent)`，交 Track B 直链确认。feed 本身**不**判 `gone_confirmed`。

> 证据：feed 数组中不存在的 id 在直链返回 not_found（见第 6 节）；feed 无墓碑字段。

---

## 6. 直链复查能否区分 reachable / explicitly_removed / not_found / restricted（清单 6）

两个直链通道，互为印证：

| 通道 | 存在公开帖 | 不存在 id | 需登录 |
|---|---|---|---|
| `show.json?id=` | HTTP 200 + 完整 JSON | HTTP **400 + `error_code=20210`「您访问的页面不存在」** | `10022` |
| HTML `xueqiu.com/{uid}/{id}` | HTTP **200** | HTTP **404** | — |

- **`reachable`**：`show.json` 200 且 `text` 可解析；HTML 200。
- **`not_found`**：`20210` / HTML 404（裸不存在）。按规范 §0.7 **记 `source_state=unavailable`，不判 `gone_confirmed`**（无法区分作者删/平台删）。
- **`restricted`**：状态字段 `is_private=true` / `is_refused=true` / `legal_user_visible=false` / 付费墙 / 被拉黑 → `unavailable`。（样本 `legal_user_visible=false` 字段已现身，具体受限码待运营中遇到真实样本补登。）
- **`explicitly_removed` → `gone_confirmed`**：仅当来源页**显式**显示「已删除/内容已被移除」时。该专属错误码/页面文案**未能在探针阶段构造**（无法主动删除他人帖）；**留作运营中首次遇到真实删帖时学习并回填本表**。在此之前，保守按 not_found→`unavailable` 处理，符合 §0.7 与 `gone_confirmed` 黏性规则（§2.3）。

> 证据：`raw/03_show_391984427.json`(200)、`raw/03_show_bad_*.json`(20210)、`raw/04_html_real.txt`(200)、`raw/04_html_notfound.txt`(404)。

---

## 7. 直链正文能否稳定提取（清单 7）

- **用 `show.json`，不用 HTML 抓取**：HTML 页仅含 `window.SNOWMAN` 与 meta，**不内嵌完整状态 JSON**，正文提取不稳。HTML 页只用于 200/404 可达性交叉校验。
- `show.json` 稳定返回 `text`（完整 HTML 正文）、`description`（摘要）、`edited_at`、`censor_state`、`is_private` 等。

> 证据：`raw/04_html_real.txt`（无完整 JSON）、`raw/03_show_391984427.json`（字段齐全）。

---

## 8. feed 与直链正文能否归一化为相同内容（清单 8）— **关键**

**能。** 结论：以 **`text` 字段为正典**，两轨可归一化为同一 `content_hash`。

- 同一帖 `391984427` 实测：
  - `text` 原始：feed 593 字 vs show 848 字（不等，HTML 标记不同）。
  - `text` 经「去标签 + 解实体 + 去**全部**空白」：feed 218 == show 218，**完全相等**。
  - `description` 始终不等（feed 152 vs show 150）→ `description` 是各接口**自行生成的摘要**，**禁止**用于哈希。
- 差异根因：两接口对 `$股票$`、`#话题#`、`@用户` 等内联锚点的 HTML 包裹不同（feed 内联、show 块级换行），仅影响空白，不影响文字。
- **采用方案**：
  - `content_text`（展示用）= `HTMLParser(text).text()` → unescape → NFC → 空白折叠为单空格、trim。
  - `content_hash` = sha256( 上述结果再**去掉全部空白** )。
  - `raw_payload` = 原始完整状态 JSON。
- **后果**：两轨 `content_fidelity=full`（普通帖），跨轨编辑检测成立，单一哈希空间。
- **代价/边界**：纯空白编辑不可见（KOL 场景可接受）；哈希对拉丁文丢词边界（仅哈希，`content_text` 保留真实文字，展示不受损）。
- **退化情形**：`is_column=true`（付费长文/专栏）或 `truncated=true` 时 feed 可能只给预览 → 该帖 feed 侧 `content_fidelity=preview`、不建版本，编辑检测退化为**仅 Track B**（直链 `show.json`）能力，须按规范 §2.4 标注。普通状态帖实测 ≤1547 字均 `truncated=false`、`text` 全量。

> 证据：`raw/cmp_feed_text.txt`、`raw/cmp_show_text.txt`、`raw/05_*`（若有截断样本）。脱敏回归样本与离线校验：`fixtures/normalization_pair.json`、`verify_normalization.py`。

---

## 9. 两轨频率限制（清单 9）

- **未做封禁阈值压测**（避免连累用户 IP、且非必要）。
- 实测：整轮 ~35 请求、间隔 1.5–2s、每会话 1 次首页引导 → **零 429、零限频错误码**。
- 接口含 `rate_limited` 语义需靠观察；目前仅见 `10022`（登录）与 `20210`（不存在），未见限频码。
- **建议节奏（温和采集，规范硬约束）**：
  - Track A feed：每账号 2–6 小时一轮，错峰 + 抖动。
  - Track B 直链：批量复查时 ≥ 2–3s/请求 + 抖动；钉住帖每日一次，`recheck_queue` 项在 TTL 内每数小时一次。
  - 每个采集 run 仅 1 次首页引导铸 cookie，复用整轮。
  - 见 `10022`/连续异常 → 软停该轮并标记账号采集健康度。

---

## 10. 监控窗口、旧帖与钉住帖复查策略（清单 10）

- **监控窗口默认 30 天**（`recent_window`）：实测采样账号 page1 覆盖 ~38 天，30 天窗口对中低频账号 guest 即可完整覆盖；高频账号需登录翻页或缩短窗口/提高频率。
- 滑出窗口且未钉住 → `out_of_scope` + `watch_mode=inactive`，停止持续监控（§0.8）。
- 钉住帖（`watch_mode=pinned`）→ Track B 每日直链复查，无视窗口。
- `recheck_queue` 项 → TTL 内由 Track B 复查，到期 `expired`。

---

## 11. 回填 tasks.md 第 5 节配置（建议初值）

| 配置项 | 建议初值 | 依据 |
|---|---|---|
| 探针平台 | `xueqiu` | 用户选定 |
| 追踪账号 | **待用户提供**（uid 或主页 URL） | 探针用的是随机公开号，非目标 |
| 登录 cookie | **建议用户提供**（`xq_a_token`/`xqat`/`u`，环境变量或本地 `config.local.yml`） | 翻页/回填/私密内容需要（§3） |
| feed 轮询频率 | 每账号 2–6h | §9 温和节奏 |
| 监控窗口天数 | 30 | §10 |
| 缺席阈值 N | 3（规范下限） | §0、§2.2 |
| 直链复查频率 | 钉住帖每日；队列项 TTL 内每数小时；≥2–3s/请求 | §9 |
| `recent_feed_absent` TTL | 7 天 | 经验初值，可调 |
| `llm_candidate` TTL | 14 天（阶段 3） | 经验初值 |
| `MIN_SAMPLES` | 30（阶段 3） | 经验初值 |
| feed `content_fidelity` | `full`（普通帖）；`is_column`/`truncated` 时 `preview` | §8 实测 |
| 直链 `content_fidelity` | `full` | §8 |
| `live_monitoring_started_at` | 系统自动写 | 规范 |

---

## 12. 影响 1b 实现的硬事实（给建库阶段）

1. **id/uid 用数字**；`posts.platform_post_id`=数字 id，`url` 由 `{uid}/{id}` 拼接，`platform='xueqiu'`。
2. **`created_at`/`edited_at` 是 epoch 毫秒**，入库统一转 ISO8601/UTC（注意时区，雪球时间为东八区本地）。
3. **正文哈希按第 8 节双重归一化**，`content_text`/`content_hash`/`raw_payload` 三者分别落（§2.4）。
4. **置顶帖按 `mark==1` 识别并排除出 covered 范围**（§2）。
5. **错误码 → 状态机**：`10022`=登录降级（健康门不过）；`20210`/HTML404=not_found→unavailable；显式删除文案=gone_confirmed（码待补）；`is_private/is_refused/legal_user_visible`=restricted→unavailable。
6. **guest 仅 page1**；`fetch_runs.pagination_complete` 据此判定；有登录 cookie 才做多页/回填。
7. **`adapter_version`** 起始 `xueqiu-1`；接口结构若变更递增。
8. **cookie 仅放环境变量或被忽略的本地 `config.local.yml`**，绝不入库、绝不进导出（规范硬约束）。

---

## 13. 待运营中补登的开放项

- 真实**删帖**的专属错误码/页面文案（区分 `explicitly_removed` 与 `not_found`）。
- 真实**受限/私密/付费**帖的错误码（确认 `restricted` 判定）。
- `is_column`/超长文的截断阈值与 feed 预览字段精确边界。
- 登录 cookie 的实际有效期与续期表现。
