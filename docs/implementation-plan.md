# 实施计划

Spec: docs/spec.md

1. 实现 Python CLI 与安全工作流：配置、Codex/Git/Markdown 证据、时间范围、三类报告、冻结预览、Codex AI 接口、DWS 适配、JSON 锁/状态、日历。编写行为测试后实现，至少覆盖日期边界、去重、隐私过滤、无确认禁止提交、模板漂移、超时未知状态、读回不一致。
2. 集成真实本机环境：读取现有 DWS 配置状态、核验模板/收件人、采集当前工作生成证据与预览；记录非文本编码是否经过官方证据验证。仅 dry-run，不实际提交。保留用户原文。
3. 交付技能、配置、Windows 运行和安装/任务脚本、中文说明。默认关闭自动提交。全仓审查、修复、测试、提交并推送用户提供的 origin/main。

接口：CLI python -m lablog；本地 config/config.local.yaml；所有运行产物 data/。字段映射依据实际 DWS 模板，真实收件人 ID 不进公开配置。私密证据和预览不进入 Git。
