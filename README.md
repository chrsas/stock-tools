# stock-tools

本地单用户 KOL 发言证据存档。当前完成阶段 1a、1b 和 1c。

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

导出目录默认为 `data/exports/export-<时间戳>/`。导出过程不读取本地配置，并会对 `notes`、
`raw_meta` 和 `raw_payload` 中常见 cookie、token、API Key 和授权头执行启发式脱敏。
帖子正文等证据字段保持原文。`data/` 已被版本控制忽略。

## 质量门禁

```powershell
.\scripts\check_quality.ps1
```
