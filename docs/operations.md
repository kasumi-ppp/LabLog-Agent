# LabLog-Agent 运维说明

配套文档：`docs/reliability-audit.md`（审查表与测试报告）。
本文回答日常运维中最常见的六个问题：数据在哪、重启后怎么恢复、未知提交怎么办、残留锁怎么诊断、怎么备份迁移、怎么查定时任务。

命令统一使用：

```bash
python -X utf8 -m lablog --config config/config.auto.yaml <命令>
```

（PowerShell 可用 `scripts\run.ps1 -Config config\config.auto.yaml <参数>`，它已补 `-X utf8`。）

---

## 1. 数据目录与配置解析规则

* **配置路径**：`--config` 若是相对路径，一律按**项目根**（`lablog/` 的父目录，即 `E:\project\钉钉`）解析，不再依赖当前工作目录。省略时默认 `<项目根>/config/config.local.yaml`。
* **数据目录**：配置文件里的 `data_dir`（相对值按配置文件所在目录解析，建议直接写绝对路径）。
* **每次运行都会输出实际使用的目录**：

  ```json
  {"status": "...", "data_dir": "E:\\project\\钉钉\\data-auto", "config": "E:\\project\\钉钉\\config\\config.auto.yaml"}
  ```

  看到 `data_dir` 不是你预期的那个，就是配置指错了，先修配置再跑。

目录结构（`<data_dir>/`）：

```
drafts/     日报草稿 daily-<YYYY-MM-DD>.json（+ .bak 备份 + history/ 快照）
state/      <周期哈希>.json  ← 提交状态的唯一权威
state/heartbeat/  每次运行的开始/结束心跳
evidence/  采集到的证据快照（含 collected_at 与来源可达性）
previews/  预览 md/json
backlog/   超限正文的结转池
diagnostics/           写失败、锁、证据拒绝等诊断记录
diagnostics/alerts/    未能发出的告警（可补发）
scheduler/ 定时任务的日志与 runs.jsonl
```

---

## 2. 重启后如何恢复

正常情况**不需要做任何事**：草稿、证据、提交状态、backlog、心跳都在磁盘上，重启后重新跑同一个命令即可。

排查顺序：

```bash
# 1) 看整体健康：数据目录、锁、未完成操作、待人工核验、漏跑
python -X utf8 -m lablog --config <cfg> doctor

# 2) 有未决提交时，只读核对远端（不会再次发送）
python -X utf8 -m lablog --config <cfg> reconcile --draft <data_dir>/drafts/daily-<date>.json
```

状态机：`draft → submitting → verified`；异常路径 `submitting → unknown / mismatch → reconciled 或 manual_review_required`。
**任何异常状态都不会自动重新提交**，只有远端读回一致才会升级为 `verified`。

---

## 3. 发生未知提交状态怎么办

症状：草稿里 `submission.status` 为 `unknown` / `mismatch`，或 doctor 的 `needs_manual_review` 列出了它。

1. **不要重投**。`submit` 会直接被拒（这是设计，不是故障）。
2. **先只读核对**：`reconcile --draft <草稿路径>`。远端读回一致 → 自动置 `verified` 并结转 backlog；不一致 → 返回 `manual_review_required`。
3. **人工核对远端**：

   ```bash
   dws --profile <profile> report +outbox-list --start 2026-10-14T00:00:00+08:00 --end 2026-10-15T00:00:00+08:00
   ```

   注意 `--start/--end` 必须是带时区的 ISO-8601；写纯日期会报 `invalid_time_format`。
4. 若确认远端已存在且内容正确，但本地仍是未知：保留现状即可（不会重复提交），或让 `reconcile` 用正确的 report id 收敛；**不要手工改 state 文件**，除非你清楚 `attempt_id` 与 `phase` 的含义。
5. 若两份文件不一致（state 与草稿说的不是同一次尝试），doctor 会报出 `inconsistency`，此时只做人工处理：要么确认远端后走 `reconcile`，要么人工核对后按第 6 节备份再清理。

---

## 4. 残留锁如何诊断

锁文件是 `目标文件.lock`，内容形如 `{"pid":..., "host":..., "token":..., "created_at":...}`。

```bash
python -X utf8 -m lablog --config <cfg> doctor   # 关注 local.locks 与 local.issues
```

* `owner_alive: false` → 持有进程已死，下次运行会**自动接管**（不做任何手工操作）。
* `owner_alive: true` → 真的有人在跑，等它结束，不要删锁。
* `owner_alive: null` → 判定不了（通常是别的主机留下的锁）。确认那台机器不在跑之后，再手工删除 `.lock` 文件。
* **不要按锁文件年龄判断**：年龄只是诊断信息，代码从不按年龄抢锁。

陈旧临时文件（`*.tmp`）同样由 doctor 的 `temp_files` 列出；持有者仍存活的临时文件不会被误判为垃圾。

---

## 5. 如何备份与迁移

备份：整个 `data_dir` 目录即可（也可只备份 `drafts/`、`state/`、`evidence/`、`backlog/`）。

```powershell
robocopy E:\project\钉钉\data-auto D:\backup\lablog\data-auto /E /COPYALL /R:2 /W:2
```

迁移到新机器 / 新目录：

1. 拷贝 `data_dir` 到新位置；
2. 配置文件里把 `data_dir` 改成新的**绝对路径**（推荐），其它键不动；
3. 跑一次 `doctor`，确认 `counts` 里草稿/证据数量与迁移前一致、`issues` 为空；
4. 跑一次 `preview --draft <某个草稿>`，确认能正常读回。

兼容说明：旧格式（v1）的 state 文件会自动迁移为 v2，原有字段一个不少；草稿不需要迁移。
**切换工作目录、换 PowerShell 窗口、换虚拟环境都不影响数据位置**——只要 `--config` 是同一个（相对路径按项目根解析）。

---

## 6. 如何检查定时任务运行结果

每次运行都会留下三条痕迹：

| 痕迹 | 位置 | 内容 |
|---|---|---|
| 专属日志 | `data/scheduler/<kind>-<date>-<HHmmss>-<pid>.log` | UTF-8，同日多次运行互不覆盖 |
| 运行流水 | `data/scheduler/runs.jsonl` | 逐行追加：run id、开始/结束时间、退出码、脚本、配置、日志路径 |
| 心跳 | `state/heartbeat/auto-<kind>.json` | 开始/结束两次写，用于发现「根本没跑」 |

常用检查：

```bash
# 最近 7 个工作日有没有漏跑、有没有未送达告警
python -X utf8 -m lablog --config <cfg> doctor --check-schedule --days 7
# PowerShell 包装
powershell -NoProfile -File scripts\check-heartbeat.ps1 -Python E:/Anaconda3/python.exe -Config config\config.auto.yaml -Days 7
```

退出码：0 = 正常（not_due / already_submitted / ready / verified）；2 = 需要人工处理（blocked / unknown / mismatch / manual_review_required / busy / error）。

---

## 7. 认证过期与漏跑

定时任务仍是「用户登录时运行」，所以**未登录或关机不会执行**；这类漏跑由 `--check-schedule` 回溯发现。

DWS token 过期时（日志里出现 `token_refresh_failed`）：

1. 告警会**先写到磁盘** `data/diagnostics/alerts/`，再尝试发信；发不出去也不会丢。
2. 重新登录 Windows 会话后执行：

   ```bash
   dws auth login    # 扫码重新授权
   ```
3. 补发此前没发出去的告警：

   ```bash
   python -X utf8 -m lablog --config <cfg> doctor --resend-alerts
   ```
4. 再手动跑一次：`auto --kind daily --date <当天>`（需要提交时再加 `--submit`）。

告警邮件正文固定包含上述 `dws auth login` 指引；日志与告警都不含凭证与会话原文。

---

## 8. 日常命令速查

```bash
# 采集证据（写 evidence/，带采集时间与来源可达性）
... collect --kind daily --date 2026-10-14

# 生成草稿（重复生成：内容不变则不动；被编辑/预览过则先快照）
... generate --kind daily --date 2026-10-14 --engine extractive [--force]

# 预览（写入 previewed_at，不改变 hash）
... preview --draft <data_dir>/drafts/daily-2026-10-14.json

# 提交（需要 --confirm-hash，且必须先预览确认）
... submit --draft <草稿路径> --confirm-hash <草稿 hash>

# 未决提交只读核对
... reconcile --draft <草稿路径>

# 无人值守一条龙（--submit 才真正提交）
... auto --kind daily --date 2026-10-14 --engine assisted --ai-result-file ai.json [--submit]

# 结转池
... backlog --show | --prune | --clear --yes

# 本地诊断
... doctor [--check-schedule --days 7] [--resend-alerts]
```
