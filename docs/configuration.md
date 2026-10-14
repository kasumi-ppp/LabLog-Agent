# 配置

先将 `config/config.example.yaml` 复制为 `config/config.local.yaml`。示例中的路径只是格式示例，需替换成本机安装路径；真实身份和收件人 ID 只填本地文件。不配置 Token、密码或 AppSecret。

本机示例环境使用 Python 3.13、官方 DWS 和 Codex 原生 exe；其他机器可先使用 `Get-Command python,dws,codex` 找到安装入口，再核实原生可执行文件。项目通过 argv 调用，不执行 .ps1/.cmd Shell 字符串。

| 配置 | 用途 |
| --- | --- |
| dws.executable/profile | 官方 CLI 路径及固定 corpId:userId profile |
| identity.corp_id/user_id/corp_name/user_name | 预期组织与执行账号，提交前重新核验 |
| recipients | 已核实的姓名、真实 user_id 和组织名 |
| templates.daily/weekly/monthly | 用户现有模板的 ID 与精确名称；运行时再次读取 |
| codex.session_roots | 允许读取的 sessions/archived_sessions 根目录 |
| codex.mode | selected 指定项目/会话；auto_work 在配置的工作来源边界内发现会话 |
| codex.project_paths/session_ids/exclude_paths | 工作项目范围、指定会话、排除个人项目 |
| supplemental.git_paths/markdown_paths | 明确允许读取的仓库和有日期的笔记，默认不扫描其他路径 |
| data_dir | 本地证据、草稿、状态与回执；默认 data，Git 已忽略 |
| date_encoding | 经权威依据核实的日期值契约；未知时保持 verified=false |
| calendar.file | 对应年度的节假日/调休日历，包含来源和更新时间 |
| schedule | 时间、周截止日、月截止日；auto_submit 必须 false |
| quality.carryover | 正文可持续发展：按主题完整性裁剪正文、溢出内容入本地 backlog 留待以后补交 |

本机首轮配置只采集当前 LabLog-Agent 项目。要整合其他 Codex 工作，将其项目目录加入 project_paths；按项目筛选仍会自动收集该项目相关会话。需要限定具体会话可再填写 session_ids。没有项目路径或会话 ID 时不采集 Codex 会话。完全一致的重复片段会合并并保留来源，计划和完成等状态不同的内容分开处理。不要把 Codex 全部会话默认视为科研工作。

`quality.carryover` 控制「正文可持续发展」。默认未配置时 `enabled: false`，生成与提交行为与开启前逐字一致。`target_chars`（默认 200）是正文目标字数，是**下限而非上限**：生成草稿时把正文按主题聚合成块整块保留直到达到该字数，绝不从句子中间截断，宁可超过也不把工作说一半。`apply_to` 默认 `["completed"]`，即只有「今日完成」参与裁剪入队；`plan_extra: true` 时「明日计划」的多余内容也入队并标记 `kind=plan`。溢出内容写入 `<data_dir>/backlog/items.json`，仅在提交被读回校验成功（status=verified）后才标记消费；`kind=plan` 的条目永远不会被用于补交。日常可用 `backlog --show` / `backlog --prune` / `backlog --clear --yes` 查看与清理。

`history_from` 是远端重复检查的最早创建时间；如需覆盖更早报告，首次生成前应把它设到足够早的日期。范围之外的历史不会被视为“没有重复”，提交会因查重覆盖不足而停止。

Markdown 记录要显式标注实际工作日期，推荐 YAML frontmatter 的 `date: YYYY-MM-DD`、`status: completed|in_progress|planned|unknown`；状态需与内容一致。文件修改日期不能证明工作发生日期，Git 提交也不能证明测试通过。真正的工时可在生成时输入 `--hours`。

日志按事件时间换算至 Asia/Shanghai，使用左闭右开的日/周/月窗口；会话创建日或最后修改日不代替每条消息的发生时间。缺失、无法读取或仍写入的来源应在 warnings 中出现。
