# stock-tools

本地单用户 KOL 发言证据存档。当前完成阶段 1 和阶段 2。

## 初始化

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m kol_archive init-db data/kol.sqlite3
```

复制 `config/config.yml` 为 `config/config.local.yml`，填写追踪账号。登录 cookie 可放在
`XUEQIU_COOKIE` 环境变量，也可放在已被 gitignore 的本地配置中。

## 运行一轮

```powershell
.\.venv\Scripts\python.exe -m kol_archive run-once --config-dir config
```

一轮包含 feed 轮询和到期直链复查。需要定时运行时，由 Windows 任务计划程序或 cron 调用该命令。
每轮成功完成后会生成 SQLite 快照，并实际恢复到临时数据库执行完整性校验。默认保留最近 30
份快照，可在 `config/config.local.yml` 覆盖 `storage.backup_retention_count`。

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

查看按最近在场观察排序的原始时间线：

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

## 质量门禁

```powershell
.\scripts\check_quality.ps1
```
