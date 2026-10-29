# 钉钉 AI 日志工作台

桌面三栏：项目/会话任务树、真实消息证据、结构化日志编辑。原生 HTML/CSS/JS，无前端构建步骤；FastAPI + SQLite 保存任务索引、人工记录与选择。

## 启动

在仓库根目录执行（Python 3.11+）：

```powershell
python -m pip install -e '.[test]'
python -m pip install -r webapp/requirements.txt
python -X utf8 webapp/run.py --config config/config.auto.yaml --db data-auto/workspace.sqlite --port 8015
```

打开 <http://127.0.0.1:8015/>。`--config` 必须与 WorkBuddy / `scripts/auto.ps1` 使用同一份配置，`data_dir` 决定共享草稿和提交记录的位置。省略配置时优先使用 `config/config.auto.yaml`，其次 `config/config.local.yaml`。没有配置也能保存人工工作草稿，但不会冒充可提交日志。

已有 WorkBuddy 自动化继续执行原命令：

```powershell
.\scripts\auto.ps1 -Python 'E:\Anaconda3\python.exe' -Config 'config/config.auto.yaml' -Kind daily
```

Web 服务不注册、不替换计划任务，不依靠网页定时器发送。现有工作日、身份、接收人、模板、查重、dry-run、提交锁与回执验证继续生效。网页显示配置及最近心跳，不把“已配置”当作调度器正在运行。

## 使用

1. 点击“同步最新会话”，同步 Codex 主会话和全部 WorkBuddy 会话目录；内部 guardian / 子 agent 不充当独立用户会话。
2. 先选择项目，再查看该项目的会话小标题。支持来源、归档/删除状态及标题筛选；按会话勾选或批量选择当前项目的筛选结果，选择跨日期持续生效。
3. 点击会话按报告周期分页阅读真实消息和工具摘要。会话数量与消息数量分开统计；消息写入完成不等于工作完成。
4. “生成草稿”按选中会话的当期证据做本地抽取，明确记录 `extractive` 引擎。原项目范围继续默认选入，其他会话需手动勾选；Git / Markdown 补充证据仍沿用各自配置。
5. 可直接编辑日志。真实模板已配置时，首次保存会读取模板并建立共享 JSON 草稿，后续使用版本检查和文件锁。进展、问题、措施映射到真实模板总结字段；清空字段同步清空实际发送值。
6. 人工保存后后台使用该版本，不重新生成覆盖它。选择改变只提示合并，不改变人工正文。没有模板时只保存工作草稿，并提示不能自动提交。
7. 预览显示正文与已绑定接收人。手动发送需再点击“确认发送当前版本”，与自动任务共用提交服务。未知结果、正在提交或已提交版本禁止修改和重复发送。

浅色/深色主题、拖动栏宽、手机折叠任务列表与 Ctrl/Cmd+K 搜索可用。布局和主题保存在浏览器，业务数据保存在服务器。

## 数据与边界

- SQLite：原有执行器的 `tasks/task_events` 与新的 `log_tasks/log_reports/log_meta` 独立，原 `/api/v1/tasks` 保持兼容。
- 共享草稿：沿用 `<data_dir>/drafts/<kind>-<date>.json`，不清空、迁移或重建既有数据和自动化配置。
- 新增索引只保存必要摘要与消息定位；源缺失时显示证据不可访问并保留摘要。
- `<data_dir>/conversations.sqlite` 是 CLI / WebUI 共用的会话目录、字节定位、短摘要和持续选择。WorkBuddy 只读原生 `workbuddy.db` 与主会话 JSONL，原始存储不会被写入。
- 可选 ChatGPT 云端会话监控使用显式 ID 白名单及 Codex 应用定期同步快照，界面支持独立来源筛选。需要应用保持运行；助手引用不会伪装成可读正文。详见 [chatgpt-monitor.md](../docs/chatgpt-monitor.md)。
- 已删除会话默认隐藏且不能选入报告；源故障保留标题和历史摘要，影响已选会话时阻止提交。
- 当前按采集事实建立索引，尚不包含完整 AI 长对话拆分、跨 Agent 语义去重及工具 diff 索引。执行记录页不伪造日志。
- 周/月报复用已有对应模板与周期逻辑；周期调度由外部执行器负责。
- 周报可启用约 200 字的跨周候补：本周优先，上周未进周报的完成事项按原日期补入，原周及下一周保留，下下周退出候选。网页与 CLI 共用同一规则，见 [weekly-carryover.md](../docs/weekly-carryover.md)。
- 设置页只读查看配置，不在网页修改授权、收件人或创建调度任务。

## 服务器访问

默认仅监听本机。局域网部署需在服务器设置 `LABLOG_WEB_TOKEN`，再使用 `--host 0.0.0.0`；浏览器首次连接输入令牌。API 校验访问令牌、跨站写入来源及本地主机名。经 HTTPS 代理时保持 Host / Origin 一致，不把令牌放进 URL。

服务器必须能访问配置中的本机会话路径；部署到另一台机器不会自动获得这台电脑的数据。

## 验证

```powershell
$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD = '1'
python -X utf8 -m pytest tests webapp/tests -q -p no:cacheprovider --basetemp=.tmp/webui-test-run
node --check webapp/frontend/app.js
```

`webapp/tests/browser_smoke.cjs` 在独立测试服务器/配置上执行浏览器回归，不调用提交接口。设置 `PLAYWRIGHT_MODULE` 指向 Playwright 包，`LABLOG_TEST_URL` 指向测试服务；默认使用已安装 Chrome 的 headless 模式。不要指向生产配置。

审查范围和验收记录见 [webui-acceptance.md](../docs/webui-acceptance.md)。

WorkBuddy 配置、接口和浏览器回归入口见 [workbuddy-collection.md](../docs/workbuddy-collection.md)。
