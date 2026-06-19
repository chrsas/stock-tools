# 阶段 2 真实验收执行计划

> 本文是阶段 2 与早期阶段 2b 的历史验收记录，保留当时的状态判断和测试数量。
> 当前首页、阶段 3 富化、市场相关观点簇和图片证据能力以项目根目录 `README.md` 为准。

用途：交给后续 Agent 在本机逐项执行、记录结果，并判断阶段 2 CLI 基线和阶段 2b 网页界面是否可以进入稳定使用期。

规范来源：`tasks.md` 第 0 节与第 6 节、`docs/architecture.md` 的钉住与队列一节、`docs/phases-archive.md` 阶段 2 与阶段 2b。阶段 2 的目标是让原始时间线、证据卡片、关注理由和单条改写训练形成可用闭环；阶段 2b 在此基础上补充轻量网页和手机私网访问。

## 执行边界

- 只处理本地单用户工具，不扩展阶段 3 的批量富化或过滤流。
- 阶段 2b 不搬迁现有目录，不拆独立前端工程，不引入 React。
- 手机访问只走 Tailscale 私网，不开放公网端口。
- 不输出、记录、提交 cookie、API Key、本地配置内容、原始采集文件或运行数据库。
- 不为了验收修改 SQLite 运行数据，不伪造自然缺席、删帖或限权样本。
- 如需修复代码，先记录问题，再做最小范围改动，并补自动化测试。
- 每完成一项，在本文末尾的“执行记录”中补充日期、结果与必要的非敏感说明。

## A. 开始前检查

- [x] 阅读 `tasks.md` 第 0 节和第 6 节、`docs/phases-archive.md` 阶段 2 与阶段 2b、`docs/architecture.md` 的钉住与队列一节。
- [x] 确认工作区状态，区分已有改动和本轮验收产生的改动。
- [x] 确认 `config/config.local.yml` 已被 Git 忽略。
- [x] 确认 `data/` 已被 Git 忽略。
- [x] 确认本地虚拟环境可用。
- [x] 运行质量门禁。

```powershell
git status --short --branch
git check-ignore config/config.local.yml data/
Test-Path .\.venv\Scripts\python.exe
.\scripts\check_quality.ps1
```

验收标准：

- `git check-ignore` 能识别本地配置和运行数据目录。
- 质量门禁中的 lint、格式检查、类型检查和自动化测试全部通过。

## B. 本地配置检查

- [x] 确认 `config/config.local.yml` 中至少有一个雪球账号。
- [x] 如需登录态，确认 cookie 来自 `XUEQIU_COOKIE` 环境变量或被忽略的本地配置。
- [x] 如需验收改写训练，确认本地配置包含 `llm.model`，并确认 `LLM_API_KEY` 环境变量存在。
- [x] 检查时只确认字段是否存在，不打印凭据值。

参考命令：

```powershell
Test-Path .\config\config.local.yml
Test-Path Env:XUEQIU_COOKIE
Test-Path Env:LLM_API_KEY
```

验收标准：

- 账号配置可供真实采集使用。
- 凭据只停留在环境变量或被 Git 忽略的本地配置中。

## C. 立即可跑的真实闭环

### C1. 采集、备份与原始时间线

- [x] 跑一轮真实采集。
- [x] 确认命令完成后生成经恢复验证的快照。
- [x] 查看原始时间线，选取一个具有完整正文版本的 `post_id`。

```powershell
.\.venv\Scripts\python.exe -m kol_archive run-once --config-dir config
.\.venv\Scripts\python.exe -m kol_archive timeline --config-dir config
```

验收标准：

- 时间线能看到三维状态、人读标签、首次观察、最后观察和检测到缺失时间。
- 人读标签同时展示 feed、来源和监控维度。
- 删帖信号使用“强信号”“弱信号”“无删帖信号”语义，不归因移除主体。

### C2. 证据卡片

- [x] 查看选定帖子的证据卡片。
- [x] 检查观察历史、版本列表、版本 diff、状态变迁、关联 run、附注和钉住状态。
- [x] 确认卡片不输出 `raw_meta`、`raw_payload` 或凭据。

```powershell
.\.venv\Scripts\python.exe -m kol_archive show-post <post_id> --config-dir config
```

验收标准：

- 页面只描述系统实际观察到的事实。
- 版本时间使用“首次观察”“最后观察”语义。
- run 附注经过脱敏。

### C3. 钉住、关注理由与取消钉住

- [x] 手动钉住一个帖子。
- [x] 为该帖子写一条关注理由。
- [x] 再次查看证据卡片，确认理由锁定到指定 `version_id`。
- [x] 取消钉住，确认近期帖子回到 `recent_window`。
- [x] 如真实归档中存在早于近期窗口的老帖，再用老帖确认取消钉住后进入 `inactive`。没有自然样本时记录为“待观察”。

```powershell
.\.venv\Scripts\python.exe -m kol_archive pin <post_id> --config-dir config
.\.venv\Scripts\python.exe -m kol_archive add-attention <post_id> `
  --reason "阶段 2 真实验收" `
  --expectation "检查版本锁定与自动钉住" `
  --config-dir config
.\.venv\Scripts\python.exe -m kol_archive show-post <post_id> --config-dir config
.\.venv\Scripts\python.exe -m kol_archive unpin <post_id> --config-dir config
```

验收标准：

- 写关注理由后，帖子自动进入 `pinned`。
- 关注理由保留创建时的 `version_id`，后续版本变化不会改写它。
- 近期帖子取消钉住后回到 `recent_window`。
- 老帖取消钉住后进入 `inactive`。C 阶段没有自然老帖时允许暂缓，既有自动化测试继续覆盖该分支。

### C4. 单条改写训练

本项需要有效的 `llm.model` 和 `LLM_API_KEY`。缺少凭据时，记录为“待配置”，不在仓库中写入密钥。

- [x] 对一个具有完整正文版本的帖子执行单条改写。
- [x] 检查改写产物包含锁定的 `version_id`、原文、改写命题、理由、模型和 prompt 版本。
- [x] 记录人工 verdict。
- [x] 确认帖子自动进入 `pinned`。

```powershell
.\.venv\Scripts\python.exe -m kol_archive rewrite <post_id> --config-dir config
.\.venv\Scripts\python.exe -m kol_archive review-rewrite <exercise_id> `
  --verdict valid `
  --config-dir config
.\.venv\Scripts\python.exe -m kol_archive show-post <post_id> --config-dir config
```

验收标准：

- 每次命令只处理一个观察版本。
- 改写产物只进入 `rewrite_exercises`。
- 改写训练锁定版本并自动钉住。
- LLM 不补造原文没有明确表达的事实。

### C5. 手动备份、恢复与导出

- [x] 手动创建快照。
- [x] 从配置的 `storage.backup_dir` 中选择最新快照，记为 `<snapshot_path>`。
- [x] 验证选定快照可以恢复。
- [x] 导出 JSON 与 CSV。
- [x] 检查导出目录不包含本地配置或凭据。

```powershell
.\.venv\Scripts\python.exe -m kol_archive backup --config-dir config
$snapshotPath = Get-ChildItem .\data\backups\kol-*.sqlite3 |
  Sort-Object LastWriteTime -Descending |
  Select-Object -First 1 -ExpandProperty FullName
.\.venv\Scripts\python.exe -m kol_archive verify-backup <snapshot_path>
.\.venv\Scripts\python.exe -m kol_archive export --config-dir config
```

如果 `storage.backup_dir` 已在本地配置中覆盖，将命令中的 `.\data\backups` 替换为实际目录。执行
`verify-backup` 时，可直接使用 `$snapshotPath`：

```powershell
.\.venv\Scripts\python.exe -m kol_archive verify-backup $snapshotPath
```

验收标准：

- 快照通过恢复验证。
- 导出中保留帖子正文证据。
- `notes`、`raw_meta` 和 `raw_payload` 中常见敏感内容经过脱敏。

## D. 需要等待多轮采集的观察项

以下项目按正常采集节奏观察。没有自然样本时记录“待观察”，保留已有自动化测试作为状态机覆盖证据。

- [x] 连续运行多轮 `run-once`，确认新观察持续追加，时间线稳定可读。
- [ ] 出现正文变化时，确认生成新版本并展示 diff。
- [ ] 出现 feed 连续健康缺席时，确认弱信号、streak 与复查队列符合预期。
- [ ] 出现直链临时不可访问时，确认 `human_label` 同时显示 `feed：在场` 与 `来源：直链当前不可访问`，并确认 `deletion_signal_level=weak`。
- [ ] 出现来源页明确移除提示时，确认只显示强信号，不归因移除主体。
- [x] 定期确认自动快照数量遵守保留上限。

建议由 Windows 任务计划程序按本地配置周期执行：

```powershell
.\.venv\Scripts\python.exe -m kol_archive run-once --config-dir config
```

## E. 收尾检查

- [x] 再次运行质量门禁。
- [x] 检查 Git 状态和差异。
- [x] 确认凭据、本地配置、运行数据库、导出文件、快照和原始采集文件均未进入提交。
- [x] 将真实验收中发现的问题记录到“执行记录”。
- [x] 更新“阶段 3 准入判断”。

```powershell
.\scripts\check_quality.ps1
git status --short
git diff --check
```

## F. 阶段 2b 网页与手机私网访问

阶段 2 CLI 基线已经通过。后续按以下顺序补充网页界面，继续复用现有 SQLite、状态机、展示投影和脱敏逻辑。F 节开发与 D 节自然样本观察可以并行推进。

### F1. 本机只读网页

- [x] 新增 `serve --config-dir config` 启动命令，默认监听 `127.0.0.1`。
- [x] 增加服务端渲染的原始时间线页面。
- [x] 增加服务端渲染的证据卡片页面。
- [x] 确认页面展示三维状态、诚实观察时间、删帖强弱信号、版本 diff、关联 run 和脱敏附注。
- [x] 确认页面不输出 `raw_meta`、`raw_payload`、cookie、API Key 或本地配置内容。
- [x] 为只读路由、脱敏和不存在的 `post_id` 增加自动化测试。

本机验收命令：

```powershell
.\.venv\Scripts\python.exe -m kol_archive serve --config-dir config
```

验收地址：

```text
http://127.0.0.1:<port>/
```

### F2. 网页写操作

- [x] 增加钉住和取消钉住入口。
- [x] 增加写关注理由入口。
- [x] 增加单条改写训练入口。
- [x] 增加记录人工 verdict 入口。
- [x] 所有修改状态的请求只接受 `POST`，并校验 CSRF token。
- [x] 写操作继续复用阶段 2 服务层，不在网页路由中复制状态机逻辑。
- [x] 为 CSRF、钉住、取消钉住、关注理由版本锁定、改写训练版本锁定和 verdict 增加自动化测试。

### F3. 手机窄屏检查

- [x] 使用浏览器窄屏视口检查时间线、证据卡片和表单。
- [x] 确认正文、状态标签和版本 diff 可读。
- [x] 确认操作按钮不会误触，表单无需横向滚动。

### F4. Tailscale 私网访问

- [ ] 在部署机器和手机安装 Tailscale。
- [ ] 配置仅允许用户自己设备访问的 ACL。
- [ ] 将网页服务监听地址显式配置为部署机器的 Tailscale 地址；默认值继续保持 `127.0.0.1`。
- [ ] 从手机通过 tailnet 私网地址打开时间线和证据卡片。
- [ ] 从手机完成一次钉住、取消钉住或写关注理由操作。
- [ ] 确认未配置公网端口映射，公网无法访问。
- [ ] 本地配置中的 Tailscale 地址和端口不进入提交。

### F5. 阶段 2b 收尾

- [x] 运行质量门禁。
- [x] 检查 Git 状态和差异。
- [x] 确认凭据、本地配置、运行数据库、导出文件、快照和原始采集文件均未进入提交。
- [x] 将网页和手机私网访问验收结果补充到“执行记录”。

## 阶段 3 准入判断

阶段 3 需要累积真实样本。满足以下条件后再评估批量 LLM 标签和过滤流：

- [x] 阶段 2 立即可跑的真实闭环已通过。
- [ ] 阶段 2b 网页界面和手机私网访问已通过验收。
- [ ] 多轮采集已经稳定运行一段时间。
- [ ] 已积累一批真实帖子、关注理由和改写训练样本。
- [ ] 用户已经确认三类标签能减少注意力噪音。
- [x] 样本不足时不引入作者分级。

当前判断：暂不进入阶段 3。阶段 2 的本地展示、关注理由、单条改写训练和备份导出闭环已验证，
但雪球 feed 与本轮钉住帖直链接口当前返回 HTML 页面，多轮自然样本也需要继续累积。

## 执行记录

| 日期 | 执行项 | 结果 | 非敏感说明 | 后续动作 |
|---|---|---|---|---|
| 2026-06-01 | A. 开始前检查 | 通过 | 本地配置和 `data/` 均被 Git 忽略；虚拟环境可用；初始质量门禁通过，64 个测试通过。 | 收尾时再次运行门禁。 |
| 2026-06-01 | B. 本地配置检查 | 通过 | 已配置 3 个雪球账号；Cookie 来自被忽略的本地配置；已配置 `deepseek-v4-flash`，`LLM_API_KEY` 来自用户级环境变量。 | 无。 |
| 2026-06-01 | C1. 采集、备份与原始时间线 | 部分通过 | `run-once` 可完成并生成经恢复验证的快照；既有真实归档时间线可读。当前雪球 feed 对 3 个账号均返回 HTTP 200 HTML 页面，归档按失败处理并禁止负面推断。 | 继续观察平台响应；已将自省附注细化为 `response_not_json`。 |
| 2026-06-01 | C2. 证据卡片 | 通过 | 使用近期完整正文样本 `post_id=2` 检查观察历史、版本、事件与关联 run；卡片未输出 `raw_meta` 或 `raw_payload`。 | 无。 |
| 2026-06-01 | C3. 钉住、关注理由与取消钉住 | 通过 | `post_id=2` 的关注理由锁定 `version_id=1`，取消钉住后回到 `recent_window`；自然老帖 `post_id=317` 取消钉住后进入 `inactive`。 | 无。 |
| 2026-06-01 | C4. 单条改写训练 | 通过 | 使用 `post_id=314` 的完整正文版本生成 `rewrite_exercise_id=1`，锁定 `version_id=305`；模型为 `deepseek-v4-flash`，prompt 版本为 `rewrite-v1`；人工 verdict 已记录为 `valid`，帖子自动进入 `pinned`。 | 后续积累更多真实改写样本。 |
| 2026-06-01 | C5. 手动备份、恢复与导出 | 通过 | 手动快照通过显式恢复验证；导出包含 JSON 和 CSV，共 16 个文件；未发现本地配置、完整 Cookie 或 Cookie 值。 | 无。 |
| 2026-06-01 | D. 多轮采集观察 | 待观察 | 连续采集稳定保守落失败证据，最新附注为 `response_not_json`；自动快照共 5 份，低于保留上限 30。当前缺少新的自然正文变化、缺席、直链不可访问和明确移除样本。 | 恢复 JSON feed 后按正常周期继续观察。 |
| 2026-06-01 | E. 收尾检查 | 通过 | 最终质量门禁通过，69 个测试通过；`git diff --check` 无空白错误；本地配置、运行数据库、导出文件、快照和原始采集文件均保持 Git 忽略状态。 | 提交时仅选择代码、测试和验收记录。 |
| 2026-06-01 | D. 多轮采集观察（继续） | 待观察 | 新增一轮真实采集：3 个 feed run 继续保守记录为 `failed`，附注均为 `response_not_json`；钉住帖 `post_id=314` 新增直链复查证据，记录为 `status=failed`、`result=unknown`、`content_fidelity=na`，未推进 `source_state` 或 `source_checked_at`。自动快照增至 6 份，低于保留上限 30。 | 继续按正常周期观察平台响应和自然样本。 |
| 2026-06-01 | E. 收尾检查（继续） | 通过 | 质量门禁再次通过，69 个测试通过；`git diff --check` 无空白错误；本地配置、运行数据库和最新快照继续保持 Git 忽略状态。 | 本轮仅保留验收记录改动。 |
| 2026-06-01 | D. 多轮采集观察（继续） | 待观察 | 再次新增一轮真实采集：3 个 feed run 继续保守记录为 `failed`，附注均为 `response_not_json`；钉住帖 `post_id=314` 新增第二条退化直链复查证据，仍未推进 `source_state` 或 `source_checked_at`。自动快照增至 7 份，低于保留上限 30。 | 继续按正常周期观察平台响应和自然样本。 |
| 2026-06-01 | E. 收尾检查（继续） | 通过 | 质量门禁再次通过，69 个测试通过；`git diff --check` 无空白错误；本地配置、运行数据库和最新快照继续保持 Git 忽略状态。 | 本轮仅保留验收记录改动。 |
| 2026-06-01 | F1-F3. 阶段 2b 网页基线 | 通过 | 新增标准库服务端渲染网页；本机时间线和证据卡片返回 `200`；修改路由的 `GET` 返回 `405`，缺少 CSRF token 的 `POST` 返回 `403`；服务只监听 `127.0.0.1:8765`。使用 Edge 窄屏渲染检查正文、状态、版本区和表单布局。 | 在用户设备安装并配置 Tailscale 后执行 F4 手机私网实机验收。 |
| 2026-06-01 | F5. 阶段 2b 收尾 | 通过 | 质量门禁通过，74 个测试通过；`git diff --check` 无空白错误；本地配置、运行数据库、截图和 Edge 临时 profile 保持 Git 忽略状态。 | F4 完成前继续保持阶段 3 暂缓。 |
| 2026-06-03 | D. 多轮采集观察（继续） | 部分通过 | 新增一轮真实采集，浏览器 CDP 通道可用；3 个 live feed run 为 `ok` 且 `healthy=True`，新增 live 帖进入归档，最新抽查 `post_id=644` 的证据卡片显示 `fetch_run_id=30`、`fetch_status=ok`、`content_fidelity=full`。作者 3 的自动回填继续以 `partial` 记录，附注为 `backfill_pages_reached`；4 条直链复查为健康 `restricted` 证据。自动快照新增 `kol-20260603T134130897597Z.sqlite3`，快照总数 10，低于保留上限 30。 | 继续等待自然正文变化、feed 连续健康缺席、直链临时不可访问和明确移除样本；阶段 3 仍暂缓。 |
