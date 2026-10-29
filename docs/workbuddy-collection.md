# WorkBuddy 工作采集与项目内会话选择

## 数据与配置

WorkBuddy 的原生目录是 `C:/Users/114514/.workbuddy`。读取 `workbuddy.db` 的 `sessions` 表取得原生 ID、标题、目录及删除标记，通过 `projects/*/<session-id>.jsonl` 读取主会话消息和真实工具摘要。SQLite 使用只读连接，不读取账号配置或修改原始会话。

在与 WebUI、自动发送共同使用的配置中启用：

```yaml
conversations:
  enabled: true
workbuddy:
  enabled: true
  root: C:/Users/USERNAME/.workbuddy
```

本机的 `config.auto.yaml` 和 `config.local.yaml` 已补充上述数据源，其他账号、接收人、发送时间和调度任务未改动。省略 WorkBuddy 的 `project_paths` / `exclude_paths` 时继承对应配置的 Codex 项目默认值；用户手动选择可以扩展默认项目，明确排除规则始终优先。`--scope-file` 对 Codex 的显式范围限制也优先于持久化选择。

## 使用与状态

左侧先选择大项目，再勾选该项目的 Codex / WorkBuddy 会话小标题。原有大项目根下的子目录合并展示；Windows 的 `\\?\` 路径与普通路径按同一项目处理，临时 WorkBuddy 工作区归入“临时会话”。标题采用原生自定义标题或原生标题，重命名不会丢失勾选。项目批量选择覆盖当前筛选结果的全部分页。

选择以“来源 + 原生会话 ID”持久保存到 `<data_dir>/conversations.sqlite`，并由网页生成和 CLI 后台共用。默认参与规则沿用既有项目范围，其他会话需手动选入。归档可筛选查看，已删除会话只能查看索引状态，不能自动生成或发送。

会话目录、消息与工具数量分别统计。工作日期按每条消息时间戳转换至 Asia/Shanghai，不使用会话创建日期替代。WorkBuddy 消息 `completed` 是写入状态；只有实际文本支持的完成声明才作为待验证成果，工具返回 completed 不会自动变成工作已完成。

中栏按报告日期分页展示原始消息与工具名称/实际状态，工具参数和完整输出不复制到页面。凭证、推理和注入系统上下文被过滤；源不可达时显示异常并保留短摘要。正文始终从原始文件按字节定位读取，本地索引每条只保存最多 600 字摘要。

文件追加采用增量游标；未完成尾行下次重试；文件截断或替换会重建该会话定位。Codex 归档若已变为 `.jsonl.zst`，以只读流解压并按逻辑字节定位，不生成原始对话副本；需要 `zstandard` 依赖。相同正文的跨日消息分别计数，重复写入的同一事件不重复计数。已保存人工草稿继续优先，选择改变只提示待合并；正式提交前检查所选来源可达性，不能靠旧缓存冒充同步成功。

## API

所有路径位于 `/api/v1/workspace`，继承现有访问令牌与同源写入保护。

| 接口 | 输入与结果 |
|---|---|
| `GET /projects` | 同步目录并返回项目 ID、名称及分来源会话数量 |
| `GET /conversations` | `project_id/source/q/include_archived/include_deleted/limit/offset`；返回 items、匹配数量和来源计数 |
| `PATCH /conversations/selection` | `changes: [{key, selected, revision}]`；批量原子保存，旧版本返回 409；selected=null 恢复项目默认值 |
| `GET /conversations/{key}/messages` | `day/kind/view/limit/offset`；view 为 messages 或 tools，返回实际记录及 next_offset；证据不可访问返回 410 |

公开结果不返回原始文件路径、cwd、工具参数或凭证。原有 `/tasks` 执行器接口和草稿/提交记录保持兼容。

## 验证

新增 `tests/test_workbuddy.py`、`tests/test_conversations.py`、`tests/test_native_log.py`、`webapp/tests/test_conversation_api.py`，覆盖原生格式、持续选择、跨日、分页、来源故障、完整日志替换、压缩归档、Windows 扩展路径、排除优先、版本冲突和缓存不保存完整正文。关闭的来源不能静默保存新选入；原生目录移除的会话保留已删除索引状态。FakeDws 集成验证网页选择传递到 CLI、人工草稿来源故障阻断、自动提交回读及防重复发送，全程无真实钉钉或邮件外发。

2026-10-15 最终回归：**180 passed**（21.51 秒）。新会话浏览器回归 10 组检查与旧界面 11 组检查均通过，页面脚本异常为 0；另有 425 条模拟会话的项目批选测试，验证 3 页读取、按 200 条分批保存及不可选项排除。只读实机目录核对识别 92 个 Codex 主会话、10 个 WorkBuddy 会话（含 1 个删除标记），与原生索引对应；未将内部子 agent 计为用户会话。规范化后共 50 个项目组，其中 3 组同时包含两种来源。实机同步得到 25 条当期任务，来源阻断为 0；补充 Markdown 的日期缺失和长度限制警告如实保留。

浏览器测试使用隔离源，不能指向正式配置：

```powershell
python -X utf8 webapp/tests/conversation_fixture.py .tmp/conversation-browser-run
python -X utf8 webapp/run.py --port 8016 --workers 0 --db .tmp/conversation-browser-run/web.db --config .tmp/conversation-browser-run/config.yaml
# 另一个终端；PLAYWRIGHT_MODULE 可指定本地 Playwright 包路径
node webapp/tests/browser_conversations.cjs
node webapp/tests/browser_project_bulk.cjs
```

夹具目录每次使用新的名称，避免重复创建原生测试表。浏览器检查项目标题、来源计数、56 条消息分页、真实工具、持续选择、人工正文保留、归档、标题搜索、网络失败及桌面/手机布局。旧配置界面另保留 `browser_smoke.cjs` 回归。
