# LabLog-Agent

把本机 Codex 工作会话、授权 Git 仓库与 Markdown 笔记整理为学生日报、周报、月报，映射到已有钉钉日志模板，并提供预览、查重和经具体草稿确认后的提交工作流。

使用官方 DWS CLI 和现有账号认证。无需企业管理员权限，也不新建钉钉 API 客户端、MCP Server 或数据库。能够生成草稿并不意味着当前账号一定拥有真实提交权限，需单独验收。

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

## 运行与范围

```powershell
python -X utf8 -m pytest -q
.\scripts\register-tasks.ps1 -Python 'python' # 注册后默认禁用，仅生成草稿
```

主要说明：[配置](docs/configuration.md)、[环境验收](docs/environment_report.md)、[Windows 调度](docs/scheduling.md)、[故障处理](docs/troubleshooting.md)。

原始会话、真实日报、个人身份配置和回执均保留在被 Git 忽略的本地目录。自动过滤不能穷尽所有私密信息，正式发送前仍需审阅摘要。当前版本不启用无人值守真实提交，不处理真实图片/附件上传。
