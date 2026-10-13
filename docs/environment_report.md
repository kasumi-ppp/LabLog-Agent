# 环境与验收报告

检查日期：2026-10-13；时区：Asia/Shanghai。身份、模板和收件人原始只读结果在本机 `data/audit/`，不进入 Git。

| 能力 | 结果 |
| --- | --- |
| Python | 3.13.9；已存在 PyYAML、pytest、jsonschema、tzdata |
| Git | 2.50.0.windows.1；用户授权新建 main 和 origin |
| DWS | 1.0.63；使用现有认证，未重复登录 |
| Codex CLI | 0.159.2；已通过 ChatGPT 登录 |
| Codex 非交互生成 | 实测模型列表请求超时，最小只读 OK 提示亦未完成；当前桌面整理可用，不能声称 CLI 后台 AI 已验收 |
| 官方钉钉技能 | 已安装 dingtalk-misc、aisearch、contact 等；实际位置为 ~/.agents/skills |
| 当前组织/账号 | 已实时核验，与用户指定目标一致；固定本地 profile |
| 收件人 | 姓名搜索多候选中精确匹配唯一，再通过通讯录详情核对；真实 ID 仅保留本地 |
| 三个模板 | 已实时读取日报、周报、月报字段并保存带时间和 profile 的缓存 |
| 发件箱 | 2026-10-01 至 2026-10-14 创建时间窗口内学生日报 0 条；该结论不覆盖所有历史日期 |
| 官方 dry-run | 文本字段请求已执行，返回 dry_run=true、executed=false；不是服务端验收 |
| 日历 | 2026 年节假日及调休已按国务院通知配置，保留来源和更新时间 |
| 真实提交 | 尚未执行，待具体草稿确认且字段检查通过 |

数字字段：与本机版本一致的官方 `internal/helpers/report_test.go` 验证 `content="3"`、type=number 和 contentType=raw 可归一化为 type="2"、contentType="origin"。应用内仍按 Decimal 校验，不能把数字字段当普通文本。

日期字段：模板确认 type=5；CLI 只验证 content 是字符串，不说明日期值应是 ISO、毫秒时间戳还是其他格式。未找到已提交样本或权威语义说明，默认保持未知，阻止真实提交。图片和附件为空时省略，不伪造资源 ID。模板返回未提供 required 属性，不能声称服务端全部字段均已验证可选。

沙盒下读取 DWS 用户目录可能发生文件锁或拒绝访问，甚至出现误导性的“未登录”。本次通过获准的只读环境检查确认实际已登录。不能据此删除认证文件或重置登录。

无人值守生成沿用当前 Windows 用户的 Codex/DWS 登录；没有将 Token 存入项目。Codex CLI 网络连接尚有阻塞，后台 AI 生成未完成验收；可先验收 extractive 采集草稿。实际重启、登出、Token 到期刷新和任务计划程序后台运行仍需要在目标机器条件下验收。系统不启用无人值守真实提交。

官方依据：[DWS Report 契约](https://github.com/DingTalk-Real-AI/dingtalk-workspace-cli/blob/4cd5d05393a8748e89d9265eb9f7ec85745a0331/skills/multi/dingtalk-misc/references/report.md)、[2026 年节假日安排](https://www.gov.cn/zhengce/zhengceku/202511/content_7047091.htm)。
