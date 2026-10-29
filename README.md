# LabLog-Agent

把本机 Codex 工作会话、授权 Git 仓库与 Markdown 笔记整理为学生日报、周报、月报，映射到已有钉钉日志模板，并提供预览、查重和经具体草稿确认后的提交工作流。

使用官方 DWS CLI 和现有账号认证。无需企业管理员权限，也不新建钉钉 API 客户端、MCP Server 或数据库。能够生成草稿并不意味着当前账号一定拥有真实提交权限，需单独验收。

周报、月报由 WorkBuddy 在本机执行的方案见 [WorkBuddy 周报、月报说明书](docs/workbuddy-periodic-reports.md)。独立入口 `python -X utf8 -m lablog.periodic` 在周日和月末到期时，冻结“日报→周报、日报＋周报→月报”的来源，接受 WorkBuddy 的 assisted 摘要，再复用预览、查重、提交和读回核验。该入口使用独立配置与 `data-periodic`，不改变现有日报；初始化默认禁止提交，需当天两种真实无写入试跑通过后启用。定时任务在 WorkBuddy 中设置，每天默认22:00启动。

## 快速开始

Python 3.11+，已有官方 DWS、Codex CLI 和钉钉官方技能。

```powershell
python -m pip install -e '.[test]'
Copy-Item config/config.example.yaml config/config.local.yaml
# 编辑本地配置中的执行文件、身份、收件人和工作项目范围；至少配置一个 Codex 项目路径或会话 ID
python -X utf8 -m lablog --config config/config.local.yaml doctor
python -X utf8 -m lablog --config config/config.local.yaml collect --kind daily --date 2026-10-13
python -X utf8 -m lablog --config config/config.local.yaml generate --kind daily --date 2026-10-13 --engine codex
python -X utf8 -m lablog --config config/config.local.yaml preview --draft data/drafts/daily-2026-10-13.json
```

改为 `--kind weekly` 或 `monthly`，会统计该日期所在自然周/月。`--engine extractive` 是本地采集检查模式，不冒充 AI 总结。AI 模式沿用 Codex 已登录账号及当前模型，输出需要来源引用；生成失败不静默降级成“AI 已完成”。桌面 Codex 也可读取 collect 证据并通过 `--engine assisted --ai-result-file <结构化总结JSON>` 生成，仍经过同样的来源和状态校验。

采集会合并标点/空白不同但正文相同、且状态一致的记录，并保留各来源引用；不同状态不会被合并。语义相近但措辞不同的片段由 AI 总结模式处理，extractive 模式不会伪称完成了语义去重。

使用 `--manual-text-file <UTF-8文件>` 保留人工提供的心得原文，`--hours <实际小时数>` 录入真实工时。未知工时不会填默认值，计划不会当成成果。

## 技能与提交

```powershell
.\scripts\install-skill.ps1 -Python 'python'
```

在 Codex 中使用 `$isyslab-report 生成今天的学生日报并展示预览`。技能安装目录默认 `~/.codex/skills/isyslab-report`，通过本地映射找到项目；已有同名技能时不覆盖。

提交前先读取完整 preview，修复 blocker，再执行 dry-run。用户明确确认该版本的完整草稿后，才使用对应 hash：

```powershell
python -X utf8 -m lablog --config config/config.local.yaml dry-run --draft data/drafts/daily-2026-10-13.json
python -X utf8 -m lablog --config config/config.local.yaml submit --draft data/drafts/daily-2026-10-13.json --confirm-hash '<已确认草稿hash>'
```

dry-run 不能证明服务端接受请求。提交结果未知时使用 `reconcile` 只读核对，不能重复提交或清空状态。日期值编码尚未核实时会阻止真实提交；周报/月报模板不允许添加收件人的语义也需要进一步核实。

## 正文可持续发展（backlog）

日报正文（「今日完成」字段）有目标字数 `target_chars`（默认 200）。开启后生成草稿时按**主题完整性**裁剪：把正文按主题聚合成块（Codex 证据用 `project`，git 证据用 `project` 或 `source_path`，markdown 证据用父目录），整块保留直到达到 `target_chars`，**绝不从句子中间截断**。溢出的整块正文写入本地 backlog（`<data_dir>/backlog/items.json`）留待以后补交；某个主题块单块就超过 `target_chars` 时整块保留、不入队。

`target_chars` 是**下限而非上限**——宁可超过，也不为了凑字数把工作说一半。

入队条目带分类字段 `kind`：`completed`（来自「今日完成」）与 `plan`（来自「明日计划」，是否入队由 `plan_extra` 控制）。当某天的日报**没有**当日完成证据（blocker `no dated completion evidence`）时，从 backlog 取 `kind=completed` 的条目拼成正文填入「今日完成」并移除该 blocker；backlog 为空或只有 `plan` 条目时保持 skipped、blocker 保留。**`plan` 条目永远不会被拿来补交**，避免「计划冒充已完成工作」。

消费时机：**只有提交并被读回校验成功（status=verified）之后**才把用掉的条目标记 `consumed`；提交失败或结果未知时**不标记**，下次还能重用。

```powershell
# 查看当前 backlog
python -X utf8 -m lablog --config config/config.local.yaml backlog --show

# 清理已消费/过期条目
python -X utf8 -m lablog --config config/config.local.yaml backlog --prune

# 清空 backlog（需显式确认）
python -X utf8 -m lablog --config config/config.local.yaml backlog --clear --yes
```

```yaml
quality:
  carryover:
    enabled: true           # 默认关闭；true 才启用裁剪+入队+补交
    target_chars: 200       # 正文目标字数（下限）
    apply_to: ["completed"] # 参与裁剪入队的字段，默认只有「今日完成」
    plan_extra: true        # 「明日计划」的多余内容也入队（标 kind=plan）
```

## 全自动（无人值守）

`auto` 子命令把整条链路串成一次运行：预检 → 采集 → 生成 → 预览 → dry-run →（`--submit`）提交 → 读回校验。
全自动链路同样走 carryover：`auto` 生成草稿时按同一规则裁剪并入队，提交成功并读回校验后消费 backlog。
任何一步受阻立即停止，**永不重试结果未知的提交**；已提交过的周期不会被重复生成。

```powershell
# 只跑到 dry-run，不提交
python -X utf8 -m lablog --config config/config.auto.yaml auto --kind daily --date 2026-10-13

# 全自动提交（日期默认取当天，Asia/Shanghai）
python -X utf8 -m lablog --config config/config.auto.yaml auto --kind daily --submit

# 注册并试跑 Windows 计划任务（时间见 schedule.auto_daily_time，默认 21:00）
.\scripts\register-auto-task.ps1 -Python 'E:\Anaconda3\python.exe' -Config 'config/config.auto.yaml' -Enable
.\scripts\auto.ps1 -Python 'E:\Anaconda3\python.exe' -Config 'config/config.auto.yaml' -Kind daily -DraftOnly
```

退出码 `0` = 正常（`not_due` / `already_submitted` / `ready` / `verified`），`2` = 需人工处理（`blocked` / `unknown` / `mismatch` / `error`）。
日志在 `data/scheduler/auto-<kind>-<date>.log`。受阻时按 `notify` 配置发提醒邮件（钉钉邮箱 + QQ）。
`register-auto-task.ps1` 只在配置里 `schedule.auto_submit_enabled: true` 时才愿意注册会真提交的任务。

无人值守可用的引擎只有 `extractive`（`codex` 需联网）；`assisted` 需外部提供 `--ai-result-file`。

### 范围接口（供 Codex / 其它 agent 调用）

当前日报监控范围（2026-10-23，见 `config/config.auto.yaml`）：采集 **`E:\project\VR` 和 `E:\Open Science`**；`E:\project\galgame news` 与 `E:\project\Comfyui` 均加入本机报告配置的排除规则。已保存或临时手动选入的 Codex / WorkBuddy 会话，以及 Git / Markdown 补充来源都不能越过排除规则。Codex 会话按 `cwd` 前缀匹配，文件来源只读取明确配置且未被排除的路径。

运行范围不写死在代码里。任何工具写一份 scope JSON 即可决定「这次运行可以看哪些证据」：

```json
{
  "project_paths": ["E:/project/VR"],
  "session_ids": [],
  "exclude_paths": ["E:/project/旧项目"],
  "git_paths": ["E:/project/VR/TBMproject"],
  "markdown_paths": ["E:/project/VR/TBM-ui-fix/docs"]
}
```

只覆盖出现的键，未出现的沿用配置文件；`project_paths` / `session_ids` 会让采集切到 `selected` 白名单模式，`session_ids` 可直接指定某次对话。
`auto` / `collect` / `generate` 均支持 `--scope-file <路径>`；计划任务会自动识别 `config/scope.json`（存在即生效，删除即回到默认范围）。
未知键或类型错误会被拒绝，实际生效的来源记录在运行结果的 `scope_source` 里。示例见 `config/scope.example.json`。

## 运行与范围

```powershell
python -X utf8 -m pytest -q
.\scripts\register-tasks.ps1 -Python 'python' # 注册后默认禁用，仅生成草稿
```

主要说明：[配置](docs/configuration.md)、[环境验收](docs/environment_report.md)、[Windows 调度](docs/scheduling.md)、[故障处理](docs/troubleshooting.md)。

原始会话、真实日报、个人身份配置和回执均保留在被 Git 忽略的本地目录。自动过滤不能穷尽所有私密信息，正式发送前仍需审阅摘要。无人值守真实提交默认关闭，仅在显式设置 `schedule.auto_submit_enabled: true` 并注册 `auto` 任务后启用；不处理真实图片/附件上传。
