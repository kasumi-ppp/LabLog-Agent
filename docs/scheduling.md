# Windows 调度

首期任务只生成本地草稿。不会真实提交，也不会把“启用定时”视为任何报告正文的授权。

先完成 doctor 与一次手动 generate/preview，再编辑本地配置的每日/每周/每月时间、周截止日和月截止日。Windows 任务触发时间使用系统本地时间，因此本机应设为中国标准时间；报告日期由脚本转换至 Asia/Shanghai。

```powershell
# 注册三个任务，默认全部禁用
.\scripts\register-tasks.ps1 -Python 'E:\Anaconda3\python.exe'

# 验收后可在任务计划程序启用，或首次注册时显式传 -Enable
# 先模拟一次调用；不会提交
.\scripts\scheduled.ps1 -Kind daily -Python 'E:\Anaconda3\python.exe' -Config 'E:\project\钉钉\config\config.local.yaml' -Date '2026-10-13'
```

任务每日触发，再由代码根据类型判断是否到期。日报检查法定假期和调休；周报按配置星期；月报按配置日期或月末。所有类型必须有该年度可信日历。默认注册不覆盖已有同名任务；需要更新时先审阅既有任务。

任务使用当前 Windows 用户、Interactive 登录类型和普通权限，不保存 Windows 密码。机器需要开机并保持该用户登录；当前版本不承诺退出登录后继续运行。可在锁屏状态下验收。并发任务由 Task Scheduler IgnoreNew 与文件锁共同防护。

运行日志在 `data/scheduler/`，不进入 Git。DWS 和 Codex 使用现有用户认证；网络、到期刷新或文件访问失败应记录为失败，不能绕过身份检查。实际任务注册、锁屏运行、重启恢复和 Token 到期刷新需在本机额外验收。

当前版本不开放无人值守真实提交。后续只有在日期编码、模板收件人语义和真实提交读回验证完成后，才实现有明确有效期、类型、工作周期和收件人范围的自动授权。

2026 年日历依据[国务院正式通知](https://www.gov.cn/zhengce/zhengceku/202511/content_7047091.htm)，不将旧年份套用到新年度。学校或实验室有特殊安排时，单独维护带来源的覆盖配置。
