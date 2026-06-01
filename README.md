# stock-tools

本地单用户 KOL 发言证据存档。当前完成阶段 1a 和 1b。

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

## 质量门禁

```powershell
.\scripts\check_quality.ps1
```
