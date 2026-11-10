# Agent Workspace Bridge

AWB 负责本机会话接力、原生会话同步、共享工作区记忆和规则分发。

- 双击 `awb_gui.pyw` 打开源码控制台。会话接力按工作空间只读搜索 Codex 和 WorkBuddy 的历史。
- 「原生会话同步」支持多选工作区、只读预览、常驻监控、合并后打开两端。两端完全退出后先备份，再合并支持的会话原文和已配对会话的归档状态。要删除两端副本，在控制台选中会话并确认。详细操作见 [原生会话同步](experiments/native_session_sync/README.md)。
- 「共享记忆」管理 `.shared/memory` 和 `.workbuddy/memory` 的链接。原有真实目录会在换成链接前备份。
- 「规则同步」按配置中的 snippet 分发规则。可先运行 `-Check` 或预演更新。

技能管理由 [Skills Manager](https://github.com/xingkongliang/skills-manager) 独立负责。

## 本地运行与构建

Python 3.10 或更新版本。将 `config.example.json` 复制为本机 `config.json` 后填好工作空间根和规则块源。旧配置文件的额外字段会被忽略。

```powershell
python -B awb_gui.py --selftest
python -B -m unittest discover -s tests
python -B build_release.py --out '<新的输出目录>'
```

构建使用本机 PyInstaller。发布包带源码脚本，原生同步在 Windows 上通过外部 Python 运行。许可证见 [LICENSE](LICENSE)。
