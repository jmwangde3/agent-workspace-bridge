# Agent Workspace Bridge

AWB 负责本机会话接力、原生会话同步、共享工作区记忆和规则分发。

- 双击 `awb_gui.pyw` 打开源码控制台。会话接力按工作空间只读搜索 Codex 和 WorkBuddy 的历史。
- 「原生会话同步」支持多选工作区、只读预览、常驻监控、合并后打开两端。两端完全退出后先备份，再合并支持的会话原文和已配对会话的归档状态。要删除两端副本，在控制台选中会话并确认。详细操作见 [原生会话同步](experiments/native_session_sync/README.md)。
- 「共享记忆」管理 `.shared/memory` 和 `.workbuddy/memory` 的链接。可保存链接状态，也可独立备份和恢复记忆正文。
- 「规则同步」按配置中的 snippet 分发规则。可先运行 `-Check` 或预演更新。更新只替换托管块，保留文件原来的编码、BOM 和块外内容。

技能管理由 [Skills Manager](https://github.com/xingkongliang/skills-manager) 独立负责。

## 本地运行与构建

Python 3.10 或更新版本。将 `config.example.json` 复制为本机 `config.json` 后填好工作空间根和规则块源。配置按 `AWB_CONFIG`、程序旁 `config.json`、`%LOCALAPPDATA%\AWB\config.json` 的顺序查找。GUI 日志写在 `%LOCALAPPDATA%\AWB\logs`。

```powershell
python -B awb_gui.py --selftest
python -B -m unittest discover -s tests
python -B build_release.py --out '<新的输出目录>'
```

构建使用本机 PyInstaller。发布包带源码脚本，原生同步在 Windows 上通过外部 Python 运行。许可证见 [LICENSE](LICENSE)。

## 共享记忆备份

「保存链接状态」生成 JSON，供「按记录修复链接」使用。旧的快照 JSON 也可以修复链接。要备份文件正文，点「备份记忆正文」，ZIP 会保存到工作区的 `.shared/memory-backups`。

```powershell
python -B share_workspace.py --workspace '项目名' --backup-memory
python -B share_workspace.py --workspace '项目名' --restore-memory '<备份 ZIP>' --restore-preview
python -B share_workspace.py --workspace '项目名' --restore-memory '<备份 ZIP>'
```

恢复前校验清单和内容。默认把恢复文件放到 `.shared/recovered-memory-*`。明确加 `--apply-memory` 后，会将缺失文件和冲突副本合并进共享记忆，再检查或重建链接。现有正文保留。

`--all` 的建立、状态和检查覆盖尚未初始化的工作区。探测、链接快照和正文备份默认只处理已初始化的工作区。可用 `--initialized-only` 缩小范围。

## 规则检查

每个托管块使用独占一行的 `<!-- name -->` 和 `<!-- /name -->`，允许空块。目标中与源无关的 HTML 注释按原文保留。遇到重复、嵌套、未闭合或错误编码时，该目标保留原样，脚本退出码为 2。`-Check` 发现差异时返回 1。`-Lint` 单独检查源。`-DryRun -Update` 预演目标变更。

更新现有目标前会生成经读回校验的同目录临时文件，并在替换时留下唯一备份。链接文件、硬链接和无法确认替换条件的文件会报错。日志中的 `BACKUP=` 和 `STAGED_FILE=` 分别记录备份和保留的临时文件位置。规则备份由用户在验收后按需清理。

规则脚本无操作参数时按 `-Status` 只读执行。只有单独指定 `-Update` 且未加 `-DryRun` 时写入。检查发现差异返回 1，参数、解析或 I/O 错误返回 2。

显式设置 `AWB_CONFIG` 时，路径和 JSON 必须有效。自检默认写入 `%LOCALAPPDATA%\AWB\diagnostics\awb_selftest.json`，也可用 `--selftest --selftest-output '<文件路径>'` 指定。崩溃日志与 GUI 日志放在 `%LOCALAPPDATA%\AWB\logs`。

记忆 ZIP 的上限为压缩包 512 MiB、10,000 个正文文件、单文件 256 MiB、正文解压总量 2 GiB，manifest 和中央目录各 16 MiB。恢复先核对元数据，再按实际读取字节和摘要验证。完成前只使用标为 `.partial` 的暂存目录。

## 原生同步候选版验收

在控制台用「兼容与恢复诊断」查看结构支持、待恢复操作 ID 和备份位置。已诊断为未知结构或待恢复的工作区会禁用写入入口，处理完后重新诊断。同步命令执行时也会独立检查。

原生同步仍受客户端私有存储结构影响。跨 SQLite 与 JSONL 的操作分步写入并逐步验证。只有现状和原计划能核对时才执行恢复。不要用备份覆盖整库，也不要在存在未完成操作时降级运行旧同步器。

```powershell
python -B -m unittest discover -s tests -v
Push-Location experiments/native_session_sync
python -B -m unittest discover -v
Pop-Location
python -B awb_gui.py --selftest
```

原生测试使用临时会话库及隔离的 Codex CLI。Windows 双客户端实际识别、续聊和恢复仍是候选版稳定发布前的验收项。
