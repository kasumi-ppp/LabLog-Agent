# WorkBuddy 周报与月报交接

## 执行边界

WorkBuddy 在装有本仓库的本机及 WorkBuddy 客户端执行；只更新现有同类 WorkBuddy 任务，若不存在才新建，避免重复任务。用户把客户端任务安排为每天北京时间 22:00。到点先检查是否到周报/月报日期；命令中的 `due` 会决定是否到期。配置和 `enable` 都不代表立即发送。任务不使用云端执行、云端存储或额外邮件；通知按 WorkBuddy 客户端现有任务设置处理。

本流程使用独立的 `config/config.periodic.local.yaml` 和 `data-periodic/`，不改现有日报配置、日报草稿或日报调度。周期任务在真实账号认证、最新模板核对和同日无写入试跑通过前保持未启用。每次必须重新核实认证与模板，不能把本地假客户端或历史结果当作真实验收。`init` 遇到配置已存在会停止，不覆盖；应先检查已有配置并保留其中状态。

2026-10-28 已通过真实账号验收并启用。两种模板都返回 `allowAddReceivers=0`，即收件人由模板固定、不允许运行时添加。已核实固定名单为赵浩哲、薛志东、王燕三人（`iSyslab@Hust`），日报收件人仍为薛志东一人。因此 `cli._preflight` 不再一律阻断 `allowAddReceivers=0`，而是要求模板在配置里声明 `fixed_recipients`，且其 user_id 集合必须等于配置 `recipients`；每一位仍逐次经 DWS 通讯录活体校验，名单不一致即阻断。注意 `dws report entry submit` 的 `--to-user-ids` 是必填项（无接收人的日志提交后对任何人都不可见），所以不能靠省略收件人绕过。

当天的同日无写入试跑：weekly 与 monthly 都在真实账号、真实最新模板下返回 `status=ready`、`dry_run=true`，随后 `enable` 成功，`periodic.enabled` 置为 true。WorkBuddy 定时任务「钉钉周报与月报定时提交」已启用，每天北京时间 22:00。首次配置未发送任何报告。历史记录见 `data-periodic/acceptance/no-write-result.json`（启用前的旧结论，仅留档）。

本周期入口退出码为 `0`（正常返回）或 `1`（阻塞或需要 WorkBuddy 提醒）；不要套用普通 `lablog auto` 的退出码。`due` 因 missed 提醒返回 `1` 时仍需查看 JSON 中今天的 kinds。

## 首次配置指令

将下面整段交给 WorkBuddy。`prepare` 输出中的路径和字段映射是后续步骤的输入；保存每条命令的 JSON 输出，并用其中的 `batch`、`job` 和 `snapshot` 路径填入下一条命令。每种类型分别保存 WorkBuddy 生成的 JSON 结果文件。

```text
请在本机仓库 E:\project\钉钉 中配置周报/月报周期流程。先查找现有同类 WorkBuddy 定时任务；找到后更新该任务，不要重复新建。只有确实不存在时才新建。任务执行位置是本机仓库和 WorkBuddy 客户端；时间设为每天北京时间 22:00。不得使用云端执行/存储，也不得另发邮件。当前周期流程未通过真实账号验收前，任务保持暂停或不启用。

先运行以下命令创建独立配置；如果配置已存在，按命令提示停止并检查，不覆盖：
python -X utf8 -m lablog.periodic --config config/config.periodic.local.yaml init --base-config config/config.auto.yaml

确认当前真实 DWS 登录账号与固定配置身份一致，并读取最新的周报、月报及上游模板。如果未登录、模板不可读或身份不符，停止后续步骤并报告阻塞原因，不能用缓存模板或模拟响应代替。

在同一天做真实账号无写入试跑。运行：
python -X utf8 -m lablog.periodic --config config/config.periodic.local.yaml due --smoke

用输出的 batch 路径按以下顺序执行。先准备周报：
python -X utf8 -m lablog.periodic --config config/config.periodic.local.yaml prepare --batch <due输出的batch路径> --kind weekly

读取 prepare 返回路径指向的冻结 snapshot 文件（文件名按输出路径为准）。只用 snapshot.facts 生成 JSON 文件，字段为 completed、summary、plan、citations；有额外字段时再提供 extra_fields。每个非空主字段的每行都必须包含 [证据:ID]，ID 必须存在于 snapshot.facts。completed 只能陈述 status=completed 的事实；plan 只能陈述 status=planned 的事实。每个额外字段严格使用 prepare 返回的 extra_fields 字段名，并只引用 categories 匹配其映射的事实。不得补造日期、工时、数字、出勤或完成状态。然后运行：
python -X utf8 -m lablog.periodic --config config/config.periodic.local.yaml finish --job <weekly prepare输出的job路径> --ai-result-file <weekly JSON文件路径>

保存 weekly finish 的真实结果后，继续独立诊断月报。smoke 不写入远端；即使周报被门禁阻断，也可以检查月报，但启用必须两者都通过：
python -X utf8 -m lablog.periodic --config config/config.periodic.local.yaml prepare --batch <同一个batch路径> --kind monthly

月报同样只基于 monthly prepare 返回的冻结 snapshot.facts 生成上述 JSON，然后运行：
python -X utf8 -m lablog.periodic --config config/config.periodic.local.yaml finish --job <monthly prepare输出的job路径> --ai-result-file <monthly JSON文件路径>

周报和月报都必须在同一天真实账号、真实最新模板下返回 status=ready、dry_run=true；再核对当前 live 模板指纹仍与两次试跑一致。任一条件不满足时保持任务未启用并报告，不要真实提交。全部条件满足后才运行：
python -X utf8 -m lablog.periodic --config config/config.periodic.local.yaml enable

enable 成功后再启用/恢复上面查到的那个 WorkBuddy 客户端任务，设为每天北京时间 22:00。首次配置不发送日报、周报或月报。
```

## 每次运行指令

```text
请在本机仓库 E:\project\钉钉 执行已配置的 WorkBuddy 周报/月报任务；只使用本机 WorkBuddy 客户端和本地文件，不启动云端任务，不发额外邮件。先运行：
python -X utf8 -m lablog.periodic --config config/config.periodic.local.yaml due

先查看 missed 和 notify_workbuddy；发现漏过的日期时通过 WorkBuddy 提醒，绝不回填这些历史周期。若 status=not_due，结束本次运行。若 status=due，按输出的 batch 路径依次处理今天的 kinds。若同日同时包含 weekly 和 monthly，必须先完整完成 weekly，再处理 monthly。

对每个到期类型运行：
python -X utf8 -m lablog.periodic --config config/config.periodic.local.yaml prepare --batch <due输出的batch路径> --kind <weekly或monthly>

若 prepare 返回 already_submitted，本类型结束并继续下一个到期类型，不生成摘要或再次 finish。若返回 blocked 或命令非零退出且有阻断原因，保留回执并通过 WorkBuddy 提醒后结束；due 因漏执行提醒而非零退出时，仍按它返回的今天 kinds 处理。

只读取 prepare 返回路径指向的冻结 snapshot 文件中的 facts 生成 JSON。每个非空字段每行都附 [证据:ID]，ID 必须属于 facts；completed 仅使用 status=completed 的事实，plan 仅使用 status=planned 的事实。summary 也只能忠实概括有证据的内容。extra_fields 只使用 prepare 返回的字段名，并保证引用事实的 categories 与返回映射匹配。周报保留可核实的阶段总结；月报优先按日归纳工作细节。跨月周报中的事实仅在具有明确目标月份日期时纳入月报；遵循快照里的去重和日期，不重复合并为多份成果。不得把缺失考勤/请假记录推断为正常出勤。ComfyUI、Galgame 及其相关排除内容不得出现在输出中。

若 snapshot 有 blockers、来源不完整、快照校验失败、AI JSON 无法通过校验、排除内容残留、真实登录/最新模板不可用，或 finish 返回 blocked/unknown/mismatch，立即停止该类型和后续类型，不发送。若同日双报，monthly 只有在 weekly 的结果已成功后才继续。成功生成并预览 JSON 后，按本类型 job 路径执行真实提交：
python -X utf8 -m lablog.periodic --config config/config.periodic.local.yaml finish --job <prepare输出的job路径> --ai-result-file <本类型JSON文件路径> --submit

finish 返回 verified 才报告为已提交并读回核验成功；already_submitted 按查重结果报告，不再提交。未知结果时保留所有本地草稿、状态、job 和 snapshot；用原 lablog 的只读 reconcile 核对：
python -X utf8 -m lablog --config config/config.periodic.local.yaml reconcile --draft <本地draft路径，可从finish结果的draft字段取得>

reconcile 未确认结果前绝不重发，不改删 state、draft、attempt 或 result 文件，不重置状态，不绕过重复提交保护。收件人语义不可改：`allowAddReceivers=0` 的模板只允许按配置声明的固定名单发送，不得在运行时增删收件人，也不得为了提高通过率去放宽这条校验。weekly 与 monthly 的执行结果、阻塞原因和本次生成的本地文件路径写入 WorkBuddy 任务结果；不要发送额外邮件。
```

## 来源与安全规则

- 周/月报告只从已提交报告和允许目录中的有效本地日报/周报草稿收集；WorkBuddy 只整理 prepare 已冻结并校验的 `snapshot.json`。不要重新采集或修改 snapshot。
- `ComfyUI`、`Galgame`、`TGGA`、官网素材、游戏素材、作品组、OP 和备用 CG 等排除内容即使残留于上游报告也不得进入事实或最终正文。输出阶段若重新引入排除项目，停止提交。
- 资料缺失只按 snapshot 的 unknown 记录表达“未找到/待补充”；绝不能把缺失记录解释为正常出勤、无请假或已完成。
- 任何来源不全、来源版本晚于 cutoff、期间/身份/模板冲突都按阻塞处理。不要用登录失败后的本地旧缓存或猜测补齐。
- 同一周期只要在 `data/`、`data-auto/` 或 `data-periodic/` 中留下 `unknown`、`submitting` 或 `mismatch` 提交尝试，生产流程的后续生成和提交都必须停止；先只读 reconcile，确认结果后再决定下一步。独立 smoke 可继续诊断模板，但不能提交。
- 月末周日可能同时到期两种报告。生产批次 monthly 的 prepare 和 finish 都要求 weekly 先成功；先提交并读回核验 weekly，再继续 monthly。smoke 的两种模板可以独立诊断，均通过才能启用。
- 未知提交结果不是失败重试信号。只调用原 `lablog reconcile` 做只读远端核对；禁止再次 `finish --submit`，直到状态由 reconcile 明确收敛。不得手改或删除状态文件。

## 本地文件

所有周期数据在 `data-periodic/`：

- `batches/`：按日期冻结的 due 批次；`scheduler/last-check.json`：最近检查日期。
- `jobs/`：每种报告的冻结任务与模板/授权信息；`snapshots/`：输入事实快照。
- `results/`：每个 job 的执行结果；`drafts/`：生产草稿；`smoke-drafts/`：无写入试跑草稿。
- `draft-archives/`：替换未提交草稿前保存的旧版本；`state/`：提交状态权威记录；`failures/`：本地阻塞回执。

周期流程使用的原始日报/周报草稿仍按配置从 `data/`、`data-auto/`、`data-periodic/` 的 `drafts/` 只读选取。不要覆盖或清理这些目录中的既有日报文件。
