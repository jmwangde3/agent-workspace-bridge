# AWB 原生会话同步实验

这是本机离线同步的实现和验证工具。自动测试只接收自己创建且有 `LAB` 标记的测试库。实机入口需要显式指定工作区。

```powershell
python -B -m unittest discover -v
```

Windows 上需要本机 Codex CLI 在 PATH 中。测试使用原生 `thread/list` 和 `thread/resume`，不发送模型请求。

## 双端原生验证

本机 WorkBuddy 5.9.3 可通过以下命令运行隔离验证。需要允许两个本地进程通过回环地址通信。测试会创建独立会话目录，并在其下保留测试日志和已校验的 `before-pair-sync.zip`。

```powershell
python -B probe_workbuddy.py 'H:/Program Files/WorkBuddy/WorkBuddy.exe' --pair
```

`PAIR_NATIVE_A_AND_B` 表示双方原生会话列表都列出了 A、B，原生历史也已加载。测试先启动两端确认门控在等待，再关闭、备份、同步和重新启动。WorkBuddy 另有原生导出、导入、重新导出和历史回放的单会话测试：

```powershell
python -B probe_workbuddy.py 'H:/Program Files/WorkBuddy/WorkBuddy.exe' --require-replay
```

## 已覆盖

- 两端各有一条会话，合并后双方都拥有两条，重复同步时继续使用同一份会话 ID 对照。
- 两端分别续写不同会话，新增原消息可以双向追加。Codex 恢复的用户和助手消息逐条与源记录比较。
- 同一会话在两端续写出不同后缀时保留两个分支。
- 模拟两端写入之间中断，重试复用原分配的 ID。第二个同步进程会被写入锁拦截。
- 关停门控等待最后一次文件写入稳定。中途重开时暂停后续写入。AWB 控制的启动入口在同步完成后才执行启动回调。
- 会话备份包括选定的消息文件与 SQLite 快照。已验证快照包含 SQLite WAL 中已提交的更改，归档可校验和读回。测试备份不收录登录文件。
- 当前仅接受完整的普通文本回合。遇到工具调用或不兼容记录会停止该实验，避免把缺内容的历史当成完整会话。

## Windows 实机验收

只读预览会报告两个原生库的读写兼容性。已验证的结构输出 `READ_WRITE_SUPPORTED`。必需字段可读但结构未知时显示 `READ_ONLY_SUPPORTED UNKNOWN_SCHEMA`，可以扫描会话，原生写入会停止。损坏或不可用的数据库分别显示 `CORRUPT_STORAGE`、`UNAVAILABLE`。摘要包含表定义、字段、唯一索引和触发器。已知表末尾新增的无约束、无默认值可空字段允许通过。客户端更新后的其他结构需先验证再加入白名单。

在两端打开同一个新的本地工作区，分别创建一条已完成的普通文本会话。先预览，确认输出 `CODEX 1` 和 `WORKBUDDY 1`。从独立 PowerShell 启动监听，然后彻底退出两端。WorkBuddy 的关闭按钮可能只隐藏窗口，需从托盘退出。保持 PowerShell 打开，出现 `SYNC_COMPLETE` 后再重开两端检查会话。

```powershell
python -B workspace_sync.py preview 'H:\路径\验收工作区'
python -B workspace_sync.py watch 'H:\路径\验收工作区'
```

`watch` 合并一次后退出。它等待相关客户端和 WorkDaddy 守护进程退出，再等待落盘稳定。写入前生成并校验本地 ZIP，内含选定工作区的历史文件和一致性 SQLite 快照。备份路径由 `VERIFIED_BACKUP` 输出。账号变化、已同步会话归档、未知上下文或不支持的会话记录会中止合并。预览和合并都只读取账号标识进行归属校验。

将 `watch` 换成 `monitor` 可在同一个 PowerShell 窗口连续运行多轮。每次检测到客户端运行后又完全退出，脚本会检查并合并。历史和索引一致时跳过备份。每轮等 `SYNC_COMPLETE` 再重开客户端，按 Ctrl+C 可结束。

在命令后可列出多个工作区。每个工作区有独立的会话映射和备份，输出按 `WORKSPACE` 区分。遇到无法处理的历史，受影响的工作区会报告 `WORKSPACE_BLOCKED`，同一轮仍会检查其他工作区。

```powershell
python -B workspace_sync.py monitor 'H:\路径\工作区一' 'H:\路径\工作区二'
python -B workspace_sync.py launch 'H:\路径\工作区一' --open both
python -B workspace_sync.py monitor 'H:\路径\工作区一' --open both
```

`launch` 在本轮所有工作区合并成功后才通过 Windows 启动客户端，`--open` 可选 `codex`、`workbuddy` 或 `both`。给 `monitor` 传 `--open` 时只在首次成功后发起启动，之后继续监控，客户端的打开时机由用户控制。命令输出 `LAUNCH_REQUESTED` 表示 Windows 已收到启动请求。一次运行一个 `watch`、`monitor` 或 `launch`。

混合工作区可加 `--compatible-only`，先用 `preview` 查看可合并数量与 `DEFERRED` 分类，再选择是否合并。工具、附件、未知格式和未完成的历史只暂缓对应会话。已有映射的会话只要一端不支持或被隐藏，就会连同对端副本暂缓。日志中的 `deferred_groups` 为暂缓的逻辑会话数，`SYNC_COMPLETE` 对应其余可合并会话。未登记的混合工作区允许存在多条普通文本会话，仍需要同工作区内可核实的 Codex 桌面索引作为导入依据。

## 归档和删除

控制台默认使用 `--compatible-only`。第一次运行会登记已配对会话两端的归档状态。以后观察到一端归档或取消归档时，会同步状态。如果只有发起端新增了可同步的续写，归档前会先补到另一端。日志的 `archived` 和 `unarchived` 统计本轮应用的状态变化。若另一端有不同的续写或历史无法安全核对，则保留两份，在 `archive_conflicts` 中计数。想继续合并正文，可以先把两端恢复为正常状态。

要从两端移除同一会话，停止常驻监控，退出 Codex、WorkBuddy 和可能运行的 WorkDaddy 守护进程。在控制台选一个工作区，点「删除两端副本」，核对会话与两端状态后确认。确认失效时重新预览。AWB 验证备份后先调用 Codex 原生删除，再把 WorkBuddy 副本移入回收站。部分删除后使用下方的恢复命令核对原计划。自动同步遇到一端缺失或已删除时只暂缓该组。

归档及删除通过本机 Codex CLI 的原生接口和 WorkBuddy 本地会话库执行，不调用模型。备份包含涉及的消息文件、原生索引和数据库，以及配对台账。备份文件保留本地。

## 诊断与恢复

操作台账保存在工作区的同步状态 SQLite 中。操作进入 `PLANNED` 后会记录备份位置，校验完成后记录其摘要。进入 `PREPARED` 时，原始映射、目标 ID、前后摘要和计划摘要会一起提交。每个写入步骤及读回验证都有持久化检查点。台账用现有会话台账中的历史重放，不另存一份消息正文或登录数据。

```powershell
python -B workspace_sync.py diagnose 'H:\路径\工作区'
python -B workspace_sync.py verify-backup 'H:\路径\工作区' --backup 'before-日期时间.zip'
python -B workspace_sync.py recovery-preview 'H:\路径\工作区' --operation '<操作 ID>'
python -B workspace_sync.py recover 'H:\路径\工作区' --operation '<操作 ID>' --confirm-recovery
```

1. 出现 `RECOVERY_REQUIRED` 时记录操作 ID。预览只读，会核对备份、计划和两端现状。
2. 确认是原操作留下的部分写入后，停止监控、退出两端，再明确执行恢复。若只完成了部分投影，会继续使用原计划的目标 ID 和消息顺序。
3. 记录尚未进入准备阶段时，下一轮可安全结束该记录。若内容、账号、备份或结构不符，AWB 会保留现场并停止。应先保留数据库、会话文件、台账和备份，再核对冲突。

新写入完成后会读回本地数据库和消息顺序，并运行 SQLite 完整性检查。SQLite 与 JSONL 是分步写入的。恢复依赖原计划和经校验的备份，直接覆盖整库可能抹掉备份之后的修改。窗口打开并实际识别会话属于单独的实机验收。Windows 进程检查与文件指纹可检测大部分并发改动，客户端没有协作锁时仍应保持完全退出。

候选版的 Codex 隔离测试使用 CLI 0.167.0-alpha.3.2。WorkBuddy 5.9.3.0 用于本机结构核对，写入测试使用临时 SQLite 和 JSONL。当前结构指纹见 `storage_schema.py`。两端真实窗口续聊及故障恢复仍需用隔离账号或隔离 HOME 验收。

## 模型上下文验证

本机模型探针使用合成对话和回环 HTTP 模型。它比较导入会话与同内容的原生对照会话，验证客户端实际送出的消息、系统指令和工具配置，并检查返回内容出现在原生会话里。

```powershell
python -B probe_model_context.py
python -B probe_model_context.py --workbuddy 'H:/Program Files/WorkBuddy/WorkBuddy.exe'
```

Codex 已验证通过，输出 `CODEX_CONTEXT_MATCH`。测试会话需要绑定目标模型提供商，Codex 会拒绝恢复提供商不匹配的会话。

未登录的 WorkBuddy 可加载历史，发送时返回认证要求。探针确认未收到任何模型请求后输出 `WORKBUDDY_SEND_AUTH_REQUIRED`，退出码为 2。单条合成会话已在实际登录的两个 WorkBuddy 账号间完成原生续聊验收。分页 Codex 历史已用本机模拟模型验证重开、追加和发送。含工具、附件或压缩历史的真实会话需要另行验收。

参考了本机 Codex 实际协议，以及 [AgentCtxSync 的 WorkBuddy 存储约束](https://github.com/westsource/agentctxsync/blob/main/docs/SUPPORTED_AGENTS.md)。实验不作为 AWB v1.4 的自动同步入口。
