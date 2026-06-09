# stock-tools

本地单用户 KOL 发言证据存档。当前已完成追加式归档、证据卡片、本地网页、批量富化、
博主市场相关观点总览，以及图片下载、OCR 和 VLM 描述基线。

## 初始化

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m kol_archive init-db data/kol.sqlite3
```

复制 `config/config.yml` 为 `config/config.local.yml`，填写追踪账号。

## 浏览器登录（默认采集通道）

雪球数据路径前置了阿里云 WAF + 滑块，纯 httpx 与 Playwright 自带 Chromium 都会被拦
（见 [probe/probe_findings.md](probe/probe_findings.md) §14）。默认采集通道改用**本机已装的真实
Edge** + CDP：先人工登录一次（必要时过一次滑块），之后复用持久化 profile。`requirements.txt`
已声明 `playwright`；**无需** `playwright install` 捆绑浏览器，因为连接的是真实 Edge。

```powershell
# 1) 启动专用浏览器（持久化 profile + CDP 端口），在弹出的窗口里登录雪球、必要时过一次滑块
.\.venv\Scripts\python.exe -m kol_archive login --config-dir config
#    等价的纯脚本启动（端口/profile 同默认值）：
#    .\start_xueqiu_browser.ps1
```

注意：Edge **首次**创建 profile 后约 10 余秒才会绑定 CDP 端口，属正常。登录窗口需**保持开着**，
随后运行 `run-once` 即可。专用浏览器配置在 `config/config.yml` 的 `browser` 段：`enabled`
切换数据通道（默认 `true` 走浏览器；置 `false` 回退 httpx 直连，已被 WAF 拦，仅离线/历史用），
`cdp_url`、`profile_dir`（被 gitignore）、`edge_path`（留空自动探测 `msedge.exe`，可手动指向
`msedge.exe` 或 `chrome.exe`）。登录态来自浏览器 profile，无需再配 cookie。

## 运行一轮

```powershell
.\.venv\Scripts\python.exe -m kol_archive run-once --config-dir config
```

默认通道下需先用上面的 `login` 启动并保持专用浏览器开着；`run-once` 会连其 CDP 端口采集。
一轮包含 feed 轮询和到期直链复查。需要定时运行时，由 Windows 任务计划程序或 cron 调用该命令
（专用浏览器须常驻；连不上 CDP 会直接报错退出，不会静默挂起）。
每轮成功完成后会生成 SQLite 快照，并实际恢复到临时数据库执行完整性校验。默认保留最近 30
份快照，可在 `config/config.local.yml` 覆盖 `storage.backup_retention_count`。

## 历史回填

实时轮询只覆盖最近 `monitoring.window_days`（默认 30 天）的滚动窗口，更早的帖子滑出窗口后
不再监控。需要为某账号建立更深的历史基线时用 `backfill`：

```powershell
.\.venv\Scripts\python.exe -m kol_archive backfill --uid <数字uid> --pages 10
# 或回翻到指定时间点为止
.\.venv\Scripts\python.exe -m kol_archive backfill --uid <数字uid> --until 2026-01-01T00:00:00+00:00
```

回填运行记为 `ingest_mode=backfill`，只做正面存档，**绝不据此推断缺席或 out_of_scope**
（宪章第 9 条：历史回填与实时监控分离；回填版本不进事件研究）。把某账号加入 `accounts` 后，
`run-once` 会自动回填一段历史作为基线：从本轮实时轮询翻到的最后一页**之后**继续翻页，直接抓更早
的帖子而不是重复请求实时已覆盖的前几页；页数与开关见 `config/config.yml` 的 `backfill` 段
（`on_add_enabled`、`on_add_pages`、`command_pages`）。若实时轮询本身已经翻到时间线尽头（短时间线
账号），则没有更早历史可回填，基线直接判定为已建立，不会再请求越界页。否则基线只有在回填**干净地**
完成计划内停止（翻到时间线尽头，或正好用完配置页数）且当轮无解析失败时才算建立；若首轮回填遇到
限流、网络故障、中断，或返回了无法解析的降级页面，后续 `run-once` 会继续重试，不会因为账号已存在
或拿到半截数据就跳过。另外，当实时轮询或自动回填撞墙（限流 / 登录失效 / 传输错误）时，会**立即结束本轮账号
循环**（同一会话共用 cookie 与域名，后续账号必然同样受阻），并跳过本轮共用的 `probe_due_posts`
直链复查——不再往同一堵墙上堆请求。

`run-once` 的实时轮询会与上一轮的覆盖范围对接：如果两轮之间出现空洞（高产账号 + 长间隔导致
本轮翻页没接上上轮最新帖），该轮记为 `partial`（`notes=coverage_gap`）并关闭本轮全部负面推断，
避免把「只是没翻到」误判成「已删除」。出现该提示说明该窗口太深，可调大 `polling.max_feed_pages`
或用 `backfill` 补齐。

## 备份与导出

手动生成快照、验证快照和恢复到新文件：

```powershell
.\.venv\Scripts\python.exe -m kol_archive backup
.\.venv\Scripts\python.exe -m kol_archive verify-backup data/backups/kol-<时间戳>.sqlite3
.\.venv\Scripts\python.exe -m kol_archive restore-backup data/backups/kol-<时间戳>.sqlite3 data/restored.sqlite3
```

导出 JSON 和逐表 CSV：

```powershell
.\.venv\Scripts\python.exe -m kol_archive export
```

导出目录默认为 `data/exports/export-<时间戳>/`。CLI 只从本地配置读取数据库路径，导出内容不会
包含本地配置。导出过程会对 `notes`、`raw_meta` 和 `raw_payload` 中常见 cookie、token、
API Key 和授权头执行启发式脱敏。帖子正文等证据字段保持原文。`data/` 已被版本控制忽略。

归档相关命令默认从 `config/config.yml` 与 `config/config.local.yml` 合并后的
`storage.db_path` 读取数据库。需要临时操作其他归档时，统一使用 `--path <数据库路径>` 覆盖。

## 原始时间线与证据卡片

查看按原帖发布时间排序的原始时间线：

```powershell
.\.venv\Scripts\python.exe -m kol_archive timeline
```

时间线展示三维状态、人读标签、删帖强弱信号、首次观察、最后观察和检测到缺失时间。强信号仅表示
来源页明确显示已移除，不归因移除主体。查看单帖观察历史、版本 diff、变迁事件、关联 run 与附注：

```powershell
.\.venv\Scripts\python.exe -m kol_archive show-post <post_id>
```

## 钉住与关注理由

```powershell
.\.venv\Scripts\python.exe -m kol_archive pin <post_id>
.\.venv\Scripts\python.exe -m kol_archive unpin <post_id> --window-days 30
.\.venv\Scripts\python.exe -m kol_archive add-attention <post_id> `
  --reason "值得持续跟踪" --expectation "关注后续兑现情况"
```

`add-attention` 默认锁定当前完整正文版本，并自动钉住帖子。可用 `--version-id` 指定已观察到的历史版本。
取消钉住后，仍在近期窗口的帖子回到 `recent_window`，已滑出窗口的帖子进入 `inactive`。

## 改写训练

在 `config/config.local.yml` 填写 `llm.model`，按需覆盖 `llm.base_url`，并设置
`LLM_API_KEY` 环境变量。每次只改写一条已观察到的版本：

```powershell
$env:LLM_API_KEY = "<本地密钥>"
.\.venv\Scripts\python.exe -m kol_archive rewrite <post_id>
.\.venv\Scripts\python.exe -m kol_archive review-rewrite <exercise_id> --verdict valid
```

改写产物只进入 `rewrite_exercises`，并自动钉住帖子。它不会进入事件研究或回测数据。

## 批量富化与市场相关观点

批量富化为每个已观察版本记录体裁、三类注意力标签、判断依据和原文片段：

```powershell
$env:LLM_API_KEY = "<本地密钥>"
.\.venv\Scripts\python.exe -m kol_archive enrich --config-dir config
```

富化按 `(version_id, prompt_version)` 幂等。可用 `--post-id` 限定单帖，或用 `--limit`
控制单轮数量。三类标签用于待处理队列和过滤流，不改变原始证据。

博主观点页只展示 `post_type=观点` 且具备明确市场关联的当前版本。市场关联由归档证据确定：

- 正文出现明确 A 股代码，例如 `SH600000`、`SZ000001` 或 `BJ430047`。
- 雪球原始载荷的 `stockCorrelation` 列表包含明确 A 股代码。
- 该版本已有人工或后续流程记录的可证伪 `claim`。

前两项在富化写入时固化为 `enrichments.is_market_related`，旧数据库初始化时一次性回填并建立索引。
页面查询只读取该标志和 `claims`，无需在每次加载时扫描全文或解析全部原始 JSON。市场无关观点仍完整
保留在富化记录和原始时间线中。

同一博主、同一明确证券代码、相邻发言间隔小于 7 天的观点会合并为一个观点簇，逐条保留首次记录、
强化、更新或相关回复。具有 `claim` 但无法归并到单一代码的观点独立展示。

## 图片证据（下载 / OCR / VLM）

帖子正文里的 K 线图、收益截图、持仓图常常和文字同等重要，且最易随删帖一并失效。
采集时帖子的图片 URL 已随 `raw_payload` 入库，但图片**字节**需要在失效前单独固化。
三个独立按需 pass（均不进实时 `run-once`，避免把慢速网络/可选依赖塞进原子写）：

```powershell
# 1) 下载并固化图片字节（sha256 + BLOB 存入 post_images，纯追加证据表）
.\.venv\Scripts\python.exe -m kol_archive download-images --config-dir config
# 2) OCR 提取图内文字（派生材料，非证据正文；winocr 主、tesseract 兜底）
.\.venv\Scripts\python.exe -m kol_archive ocr-images --config-dir config
# 3) 多模态模型描述图片（推断产物，绝不写回证据正文）
$env:LLM_API_KEY = "<本地密钥>"
.\.venv\Scripts\python.exe -m kol_archive enrich-images --config-dir config
```

均支持 `--post-id` 限定单帖、`--limit` 限本轮数量。

图片现在纳入**版本判定**：`content_hash` 只覆盖去标签后的正文，看不见图片，因此每个版本另算
一个 `image_manifest_hash`（对去签名后的有序图片 URL 列表取哈希）。正文不变但**换图/增图/删图**
也会生成新版本，被时间线和 diff 捕获。升级前的旧版本 manifest 为 NULL，按「不可比」处理——升级后
首轮不会把所有帖误判成「图片变了」，投影会把 manifest 补上，之后比对自然生效（适配器版本 `xueqiu-3`）。

`post_images` 是**纯追加日志**：一次抓取（成功或失败）记一行，同一 URL 字节被偷换时重下会追加新行、
新 sha256 并在 `notes` 标 `bytes_changed`，因此替换可被发现而无需改动旧行。单图大小、单批累计字节
上限和下载节奏见 `config/config.yml` 的 `images` 段。OCR 表记 `engine/engine_version/image_sha256`；
VLM 描述走 `vision_model` / `vision_prompt_version`（默认复用 `llm.model`），按 `UNIQUE(image_id,
model, prompt_version)` 幂等，发送给模型的是库内 BLOB 转 base64，不碰可能失效的远程 URL。

OCR 依赖是可选的，按需安装：`.\.venv\Scripts\python.exe -m pip install -r requirements-ocr.txt`
（Windows 走 winocr；跨平台用 tesseract，需另装其二进制与中文语言包）。导出时图片字节与 OCR 原文
作证据保留，VLM 描述按 `notes` 类脱敏，图片 `source_url` 的签名查询参数被剥除。

## 本地网页

启动服务端渲染网页：

```powershell
.\.venv\Scripts\python.exe -m kol_archive serve --config-dir config
```

默认地址为 `http://127.0.0.1:8765/`。首页是**博主最近观点**：左侧选择博主，右侧展示最近
10 个市场相关观点簇及已记录的市场结果。首页不展示按账号的命中率或排名。

其他视图一键可达：

- `/?view=queue`：待处理注意力队列。当前版本命中富化标签、尚未钉住且未写关注理由的帖子，
  按标签命中数与最后观察时间排序。
- `/?view=raw`：原始时间线，始终保留全部帖子证据。
- `/?view=filtered`：命中任一注意力标签的过滤流。
- `/?view=pinned`：已钉住帖子。
- `/authors/<uid>`：单个博主的观点簇、市场变化与最近帖子。
- `/posts/<post_id>`：单帖证据卡片。

证据卡片、钉住、取消钉住、关注理由、单条改写训练和人工 verdict 均保留。所有写操作都使用
`POST` 并校验 CSRF token。证据格诚实显示稀疏态，不用空结果制造确定性。

命令行也能直接看队列与账号标签构成诊断汇总（JSON 输出）：

```powershell
.\.venv\Scripts\python.exe -m kol_archive queue            # 待处理队列（--tier3-only 只看三标签命中）
.\.venv\Scripts\python.exe -m kol_archive scorecards       # 每账号标签计数与体裁构成（诊断，不排序、无命中率）
```

`scorecards` 保留为 CLI 诊断命令，按账号 id 排列，不计算命中率百分比，也不构成排行榜。

手机访问只走 Tailscale 私网。在被 Git 忽略的 `config/config.local.yml` 中将
`web.bind_host` 显式覆盖为部署机器的 Tailscale 地址，可按需覆盖 `web.port`。服务拒绝
`0.0.0.0`、`::` 等通配监听地址，不配置公网端口映射。

## 质量门禁

```powershell
.\scripts\check_quality.ps1
```
