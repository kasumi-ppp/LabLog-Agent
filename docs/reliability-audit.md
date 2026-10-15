# LabLog-Agent 持久化可靠性专项审查与修复

审计日期：2026-10-14（Asia/Shanghai）
范围：`lablog/`（workflow、cli、config、sources、ai、report）、`scripts/*.ps1`、`tests/`
原则：先审计后修改；P0 → P1 → P2；不新增第三方依赖；不触发任何真实钉钉提交；不删除 `data*/` 下既有文件。

---

## E. 验收结论（先给答案）

| 问题 | 结论 |
|---|---|
| 1. Agent 重启后能否恢复全部关键状态？ | **能**。草稿、证据、提交状态、backlog、心跳、未送达告警全部落盘；提交状态以 `state/` 为唯一权威，重启后 `reconcile` 只读核对远端即可判定，不会重新提交。 |
| 2. 会不会丢失已生成的日报？ | **不会（除一种情况）**。草稿写入前会把上一份有效副本存 `.bak`，覆盖前对「人工编辑过 / 已预览过」的草稿先做 `drafts/history/` 快照；主键损坏时自动回退 `.bak`，损坏件改名 `.corrupt-<ts>` 保留。**例外**：在「源全部不可达」时拒绝采集结果覆盖（retain），此时旧证据保留、新结果写入诊断。 |
| 3. 会不会重复提交？ | **不会**。state 文件存在即代表请求可能已发出，此后再无自动提交路径；`unknown` / `mismatch` / `submitting` 只走只读远端核对；`_auto` 不再把 `submitting` 当成功。 |
| 4. 定时任务失败是否可以追踪？ | **可以**。每次运行有唯一 run id 日志 + `data/scheduler/runs.jsonl` 追加记录；心跳文件用于发现「根本没跑」；告警先落盘 `data/diagnostics/alerts/` 再发信，发不出去可 `--resend-alerts` 补发。 |
| 5. 能否在 Windows 长期稳定运行？ | **可以**，前提是 DWS 授权有效。残留锁会被自动接管（仅当持有进程被证明已死）、原子写有备份与自愈、日志 UTF-8 且互不覆盖。 |
| 6. 哪些场景仍需人工介入？ | ① `unknown` / `mismatch`（远端结果不确定）；② 两份状态不一致（`attempt_id` 不同、镜像强于权威）；③ 跨主机/网络盘上的残留锁；④ pid 复用导致存活判定偏保守；⑤ DWS token 过期（`dws auth login`）。 |

---

## A. Bug 审查表

严重度：P0 会丢数据或误提交；P1 会丢数据或不可追查；P2 只影响可观测性。

| # | 问题 | 位置 | 触发条件 | 影响范围 | 严重度 | 是否复现 | 修复方案 | 验证结果 |
|---|---|---|---|---|---|---|---|---|
| P0-1 | 残留锁永久阻塞 | `lablog/workflow.py:66-87` `file_lock()` | 进程被强杀 / 任务超时 / 断电，`.lock` 残留 | 该工作周期后续 generate/submit 永久 busy，当天日报做不出来 | P0 | 是（子进程 kill 后复现） | 锁文件改为 JSON 持有者记录（pid/host/token/时间/命令），pid 存活探测（Windows `OpenProcess`+`GetExitCodeProcess`，POSIX `os.kill`），**仅当证明持有进程已死**才接管 | `test_stale_lock_is_reclaimed_after_the_holder_is_killed` 通过 |
| P0-2 | 释放锁不校验所有权 | 同上的 `finally: lock.unlink()` | 并发下锁易主 | 删掉他人持有的锁 → 两进程同时进入临界区 | P0 | 是（构造性验证） | `release_lock()` 只在 token 一致时删除 | `test_release_never_deletes_another_owners_lock` 通过 |
| P0-3 | 锁超时语义错误 | `workflow.py:77` 抛 `SubmissionBlocked` | 锁被占用 >5s | 被当作业务阻断并发告警邮件 | P0 | 是 | 新增 `LockBusy(SubmissionBlocked)`；`main` 输出 `status=busy`，`_auto` 遇 busy 不告警 | `test_lock_busy_is_also_a_submission_blocked` 通过 |
| P0-4 | 原子写不完备 | `workflow.py:44-63` | 序列化异常、fsync/替换失败、崩溃 | `.tmp` 残留不清理；无目录 fsync；主键损坏无备份 | P0 | 是（注入 `os.replace` 失败） | 序列化前置；`mkstemp`+uuid 唯一临时名；失败清理；目录 fsync best-effort（Windows 跳过）；`os.replace` 退避重试；写前 `.bak`；`load_json_durable()` 回退 | 4 个用例通过（唯一临时名/替换失败/不可序列化/备份回退） |
| P0-5 | 提交事务跨文件不一致 | `cli.py` `_submit()`（先写 state 再写 draft 再发请求） | 两次写之间崩溃 | state=submitting 而草稿无 `submission`；`_auto` 只读草稿 → 重新生成，state 仍在 → 提交被拒，死结 | P0 | 是（构造 state） | 新增 `lablog/state.py`：state 为唯一权威（attempt_id/phase/history/state_version/checksum），`load_period()` 判一致，`_submit` 用 `multi_lock` 定序 | 6 个用例通过（含两份状态不一致三组参数） |
| P0-6 | `submitting` 被当成已完成 | `cli.py` `_auto()` 幂等分支 | 提交中崩溃后重跑 | 退出码 0、不发邮件、不做远端核对，看似成功 | P0 | 是 | `submitting`/不一致 → 调用**只读** `_reconcile` 探测；命中 verified 才算完成，否则 `manual_review_required` + 邮件 | `test_restart_after_interrupted_submit_is_reconciled_without_resending` 通过 |
| P0-7 | 缺「请求是否已发出」判据 | `_submit` 无 phase 概念 | 任意中断 | 无法区分「预检失败可重试」与「请求已发出」 | P0 | 是 | 不变量：state 文件不存在 ⇒ 请求从未发出（可安全重试）；存在 ⇒ 永不重投；写入 `state.py` 注释 | `test_abort_before_the_request_leaves_no_attempt_and_allows_retry` 通过 |
| P0-8 | 写失败无诊断留痕 | `atomic_*` 直接抛 | 任何写失败 | 事后无法定位 | P0 | 是 | 失败写 `data/diagnostics/write-*.json`（诊断写入自身永不抛） | `test_failed_replace_keeps_the_last_valid_file_and_no_temp` 通过 |
| P1-9 | 草稿覆盖保护不足 | `cli.py:490-526` `_generate()` | 重复生成 / 人工编辑后 / 预览后 | 人工编辑与已预览草稿被静默覆盖；重复运行无明确结果 | P1 | 是 | `previewed_at/preview_count` 由 `_preview` 写入（均在 hash 排除集内）；hash 自洽检测人工改动；策略矩阵（不变→不动；编辑/预览→先快照到 `drafts/history/` 再写；已尝试提交→拒绝；`policy.draft_overwrite: protect` 需 `--force`） | 5 个用例通过 |
| P1-10 | 空结果覆盖有效证据 | `cli.py` `_collect()` 无条件覆盖 | 会话盘未挂载 / git 路径移动 / scope 配错 | 0 条事实覆盖当日已有证据 → 日报变空 | P1 | 是（删除 markdown 目录） | 覆盖守卫：任一已配置源不可达时，证据集**不允许变小**；retain 时旧文件字节不动，新结果写 `diagnostics/evidence-*`；全部可达时的真实结果照常写入（保留「主动缩小范围」语义） | 3 个用例通过 |
| P1-11 | 证据无采集时间/新鲜度 | 同上 | — | 无法区分当日新采与过期缓存 | P1 | 是 | 证据文件加 `collected_at`/`collector`/`sources[]`(configured/reachable/missing_paths/fact_count)/`deduped_fact_count`/`retention`/`checksum` | `test_evidence_records_collection_metadata` 通过 |
| P1-12 | `--config` 默认相对路径 | `cli.py` `main()` | 从不同工作目录 / PowerShell / 技能启动 | 指向别的配置与 data_dir，表现为「数据消失」 | P1 | 是 | 相对配置一律按项目根（`lablog/` 的父目录）解析；每个命令输出 `data_dir` 与 `config`；`doctor` 校验目录可写 | 2 个用例通过 |
| P1-13 | 定时任务脚本缺陷 | `scripts/scheduled.ps1`、`auto.ps1`、`run.ps1` | 同日重跑、非 UTF-8 输出、相对配置 | 日志互相覆盖、日志不可读、配置指错 | P1 | 是（静态复核） | 日志带唯一 run id（日期-HHmmss-pid）、UTF-8 落盘、配置转绝对路径、`runs.jsonl` 追加（开始/结束/退出码/阶段）、`run.ps1` 补 `-X utf8` | 见「脚本验证」小节 |
| P1-14 | 无心跳/漏跑与认证失效闭环 | 无 | 未登录时任务不跑；token 过期时告警邮件也发不出 | 失败无人知晓 | P1 | 是 | 心跳 `state/heartbeat/<task>.json`（start/finish 两次写）；`doctor --check-schedule` 回溯漏跑；告警**先落盘**再发信，`doctor --resend-alerts` 补发，正文含 `dws auth login` 指引 | 5 个用例通过 |
| P1-15 | AI 失败覆盖草稿 | `cli.py` `_generate()` | codex 生成失败 | 现状：失败发生在写文件之前，旧草稿实际保留（**无 bug，缺保护**） | P1 | 是 | 补回归测试锁定该行为 | `test_ai_failure_keeps_the_existing_draft` 通过 |
| P2-16 | `doctor` 只有远端检查 | `cli.py` doctor 分支 | — | 本地问题（残留锁/损坏文件/未完成操作）不可见 | P2 | 是 | 新增 `lablog/doctor.py`：数据目录与可写性、残留锁（含持有者存活）、陈旧 tmp、draft/state 配对一致性、证据新鲜度、待人工核验、漏跑、未送达告警；`main doctor` 合并输出在 `local` 字段 | 3 个用例 + 实机冒烟通过 |

---

## B. 代码改动清单

### 修改

| 文件 | 核心改动 |
|---|---|
| `lablog/workflow.py` | 重写持久化物理层：`process_alive()`（Windows `OpenProcess`+`GetExitCodeProcess`，POSIX `os.kill`）、`acquire_lock/release_lock/file_lock/multi_lock`（持有者 token、所有权校验、定序防死锁）、`LockBusy`、`atomic_json/atomic_text`（唯一临时名、失败清理、目录 fsync、`os.replace` 退避、`backup=.bak`）、`with_integrity/check_integrity`、`load_json_durable()`（回退 `.bak`，损坏件改名 `.corrupt-<ts>`）、`record_diagnostic()`、`sweep_temp_files()`；`draft_hash` 排除集增加 `preview_count`（既有 hash 值不变） |
| `lablog/cli.py` | `_submit`（state 优先 + `attempt_id/phase/history` + `multi_lock` + 崩溃矩阵）、`_reconcile`（读权威状态、只读取远端、可前向修复镜像、幂等消费 backlog）、`_auto`（submitting 走只读探测；unknown/不一致告警；busy 不告警；心跳）、`_generate_impl`（草稿覆盖保护与快照）、`_collect`（证据元数据 + 覆盖守卫 + 串行化）、`_preview`（写 `previewed_at`）、`_notify_outcome`（告警先落盘再发信）、`main`（配置按项目根解析、输出 `data_dir`/`config`、`generate --force`、`doctor --check-schedule/--resend-alerts`、`LockBusy` → `status=busy`） |
| `scripts/scheduled.ps1` | 去掉 `*>`（UTF-16）；日志唯一 run id；UTF-8；相对 `--config` 转绝对；`runs.jsonl`；前置校验 Python/配置存在 |
| `scripts/auto.ps1` | 同上（日志唯一化、`runs.jsonl`、绝对路径校验） |
| `scripts/run.ps1` | 补 `-X utf8` |
| `scripts/register-tasks.ps1` | 仅加注释说明未登录场景由 `check-heartbeat.ps1` 兜底（登录类型保持 Interactive） |
| `skills/isyslab-report/scripts/lablog.py` | 调用补 `-X utf8` |

### 新增

| 文件 | 作用 |
|---|---|
| `lablog/state.py` | 单一权威提交状态（读写、v1→v2 迁移、相位转换、一致性判定 `load_period`、阻断理由、心跳、告警落盘/补发） |
| `lablog/doctor.py` | 只读本地诊断（数据目录、锁、临时文件、draft/state 配对、证据新鲜度、漏跑、未送达告警） |
| `scripts/check-heartbeat.ps1` | 包装 `doctor --check-schedule [--resend-alerts]` |
| `tests/conftest.py` | 把 pytest 临时目录固定到项目内 `.tmp/pytest`（沙箱下系统 Temp 会 WinError 5） |
| `tests/test_reliability.py` | 锁、原子写、崩溃恢复、提交事务（24 例） |
| `tests/test_drafts.py` | 草稿覆盖保护、证据守卫（9 例） |
| `tests/test_operations.py` | 心跳、告警、漏跑、doctor、配置解析（9 例） |

### 未改动

`lablog/report.py`、`lablog/dws.py`、`lablog/ai.py`、`lablog/backlog.py`、`lablog/notify.py`、`lablog/config.py`、`config/*.yaml`、`data*/` 既有文件——模板映射、收件人校验、日期编码、carryover 逻辑一律不动。

---

## C. 测试报告

### 命令

```bash
cd /e/project/钉钉
mkdir -p .tmp/pytest
TMPDIR=$PWD/.tmp/pytest TEMP=$PWD/.tmp/pytest TMP=$PWD/.tmp/pytest \
  E:/Anaconda3/python.exe -X utf8 -m pytest -q -p no:cacheprovider
```

（`tests/conftest.py` 已把临时目录固定到项目内，命令行变量是双保险；`-p no:cacheprovider` 避免沙箱下写 `.pytest_cache` 的告警。）

### 结果

| 阶段 | 通过 | 失败 | 说明 |
|---|---|---|---|
| 修改前基线 | 80 | 1 | 失败项为既有夹具缺陷 `test_possible_remote_duplicate_blocks_before_submit`（假 outbox 的 `detail` 写死 `2026-10-13`，与草稿周期不匹配导致查重未触发），与本次改动无关 |
| 修改后 | **123** | **0** | 新增 42 个用例全部通过，无回归；该夹具缺陷已改为相对 today 构造周期，全量 123 passed |

测试文件分布：`test_lablog.py` 81（原有）、`test_reliability.py` 24、`test_drafts.py` 9、`test_operations.py` 9。

### 并发与故障注入

| 场景 | 注入方式 | 结果 |
|---|---|---|
| 双进程争同一份日报 | `subprocess` 起第二进程持锁 + gate 文件同步 | 后者 `LockBusy`，草稿 JSON 始终合法，释放后可正常写入 |
| 强杀持锁进程 | `Popen.kill()` | 锁文件残留 → 新进程证明其已死后接管，token 变化 |
| 活进程持锁 | 伪造 pid=当前进程 | 超时 `LockBusy`，锁文件不被删除 |
| 空/旧格式锁文件 | 写入空文件、纯 pid 文本 | 一律按「占用」处理，超时报错 |
| `os.replace` 失败 | monkeypatch 抛 `OSError` | 主键内容不变、无 `.tmp` 残留、生成 `write-*.json` 诊断 |
| 不可序列化负载 | 循环引用 dict | 写盘前失败，原文件不变 |
| 主键内容损坏 | 手工写入非法 JSON | 回退 `.bak`，损坏件改名 `.corrupt-<ts>`，主键恢复为有效内容 |
| 提交中 KeyboardInterrupt | 假 DWS 抛中断 | 记录 `unknown`，二次提交立即被拒，`submits == 1` |
| 提交前预检失败 | monkeypatch `_preflight` | 不产生 state 文件（= 请求从未发出），修复后可成功提交 |
| 证据源消失 | 删除 markdown 目录 | 旧证据字节不变，`retention.action == "retain"`，生成 `evidence-*.json` |
| AI 生成失败 | monkeypatch `synthesize` | 旧草稿字节不变 |

### 持久化恢复结果

| 场景 | 结果 |
|---|---|
| 正常生成并重启 | 草稿、证据、预览均可原样读回 |
| 写入中异常退出 | 最后一份有效数据（`.bak`）可恢复 |
| 重复生成同一天 | 内容不变则不写盘（`action=unchanged`）；被编辑/预览过则先快照 |
| 跨日期 / 跨周 | 相邻 daily 互不阻塞；同一周（10-12 与 10-16）共享 state，第二次被拒 |
| 数据迁移（v1 state） | `phase=completed`、原字段一个不少，`_auto` 直接判定已提交且不发信 |

### 脚本验证

```bash
powershell -NoProfile -File scripts/auto.ps1 -Python E:/Anaconda3/python.exe -Config config/config.auto.yaml -DraftOnly
powershell -NoProfile -File scripts/check-heartbeat.ps1 -Python E:/Anaconda3/python.exe -Config config/config.auto.yaml -Days 7
```

日志形如 `data/scheduler/auto-daily-2026-10-14-210000-12345.log`，同日多次运行互不覆盖；`data/scheduler/runs.jsonl` 逐行追加 run id、开始/结束时间、退出码。实机已用临时配置验证 `generate`/`preview`/`doctor --check-schedule` 全链路（未执行 `--submit`）。

### 剩余未解决风险

1. **pid 复用**：Windows 上 pid 被回收时可能误判存活，表现为超时报错（不会误删数据）；`doctor` 会列出 pid 供人工判断。
2. **跨主机/网络盘锁**：只比对主机名、不探测 pid，默认不抢占，需人工确认后删除。
3. **未登录不运行**：计划任务仍是 Interactive 登录，关机/未登录期间的漏跑只能回溯发现（下次登录首跑或手动 `doctor --check-schedule`）。
4. **目录项持久化**：Windows 无法 fsync 目录，`os.replace` 的目录项持久性依赖 NTFS 行为，断电场景未做额外保证。
5. **poll 依赖墙钟**：`os.replace` 退避重试依赖 `time.sleep`，与锁超时共用同一时钟；极端时钟跳变场景未验证。
6. **（已消除）夹具周期缺陷**：`test_possible_remote_duplicate_blocks_before_submit` 曾因假 outbox 的 `detail` 写死 `2026-10-13` 而恒失败；已改为相对 today 构造工作周期（`tests/test_lablog.py` `_ready_submission` 与 `test_old_report_in_another_period_does_not_block_new_period`），当前全量 123 passed / 0 failed。
