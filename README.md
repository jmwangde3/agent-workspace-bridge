# agent-workspace-bridge (AWB)

> 让多个 AI 编码 agent 共享同一个工作空间：技能按工作空间隔离、记忆与技能落盘即见、规则一次分发到多个客户端。

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

## 解决什么问题

一个人在同一台机器上会同时用 WorkBuddy / CodeBuddy、Claude Code、Codex、Qoder、Trae、Gemini CLI 等多个客户端，并且按**工作空间**（任务文件夹）分类任务，比如游戏工作空间只处理游戏、网络工作空间只处理网络。痛点有三个：

1. **技能清单全局污染** —— 每个客户端把全部技能挂在用户级目录里，A 工作空间会话里能看到 B 工作空间的专属技能。
2. **同一个逻辑工作空间，各客户端各存一份记忆** —— 在 WorkBuddy 的 `example-game` 里写进度，切到 Codex 的 `example-game` 看不到。
3. **规则文件按客户端复制 N 份** —— 改一次要同步十几个 `AGENTS.md` / `CLAUDE.md` / `GEMINI.md` / `.mdc`，必然漂移。

AWB 的答案：**不引入服务、不装依赖、不实时协同**。所有客户端的同一逻辑工作空间本来就在同一个物理目录（Codex 的 `projectlessWorkspaceRoot` 指向工作空间根，会话 cwd 就是它），所以只需要：

- 把技能实体收拢到一个 **canonical 目录**，各客户端的项目级 skills 目录做成链接指过去 → 一处增删，落盘后所有客户端立即可见；
- 把跨客户端规则写进一份 **snippet**，分发到各客户端的规则文件；
- 用一张**分配表**按工作空间名决定该空间能用哪些技能。

## 架构

```
~/.workbuddy/skills/                      全局级技能（所有工作空间可见）
~/.workbuddy/skills-vault/                全量 18 个技能保管库（宿主不扫描）

<workspace>/.shared/skills/               canonical：该工作空间专属技能实体
<workspace>/.shared/memory/               canonical：MEMORY.md 索引 + YYYY-MM-DD.md 日记
        ▲
        │ Junction / symlink（同一 inode，落盘即见）
        ├── .codebuddy/skills   .workbuddy/skills   (WorkBuddy / CodeBuddy)
        ├── .agents/skills      .codex/skills       (Codex)
        ├── .claude/skills                          (Claude Code)
        ├── .qoder/skills                           (Qoder)
        ├── .trae/skills                            (Trae)
        └── .workbuddy/memory                       (WorkBuddy 会话记忆 → .shared/memory)

skills-workspaces.json  ── 单一事实源：global + bundles + rules（按工作空间名正则）
        │  scope_skills.py --apply
        ▼
   canonical 内容重写 → 所有 agent 同时可见
```

三层可见性：

| 层 | 落点 | 范围 |
|---|---|---|
| 全局级 | `~/.workbuddy/skills/` | 所有工作空间 |
| 工作空间专属 | `<workspace>/.codebuddy/skills/`（及各家项目级目录） | 只有该工作空间，cwd 不同 → 扫描根不同 |
| 内置 / 连接器 | 宿主自带 | 全局 |

## 前置条件

- Python 3.8+（只用标准库）
- Windows 10/11：目录链接用 `mklink /J`（Junction，免管理员）；单文件共享用 hard link。脚本优先尝试 `os.symlink`，失败才回退 `mklink /J`，因此 Linux/macOS 上 `os.symlink` 可用的环境同样能跑
- PowerShell 5.1+（仅 `sync-agent-memory.ps1` 需要）
- 一个「技能真实目录」（各技能目录里都有 `SKILL.md`），以及一个不被引擎扫描的保管库目录

## 快速开始

```bash
git clone https://github.com/<OWNER>/agent-workspace-bridge.git
cd agent-workspace-bridge

# 1. 写本机配置（config.json 被 .gitignore 忽略，不会提交）
cp config.example.json config.json
#    按你的机器改：workspace_root（工作空间根目录）、config_dir（分配表所在目录）、
#    skills_vault（保管库）、skills_user（全局技能）、skills_real（技能真实目录）

# 2. 把你的技能分配表放进 configs/
#    configs/skills-workspaces.json 是示例，按你的技能名改 global / bundles / rules

# 3. 按工作空间分配技能（写全局级 + 各工作空间专属）
python scope_skills.py --status
python scope_skills.py --apply

# 4. 给一个工作空间建共享层（Junction + canonical 记忆）
python share_workspace.py --workspace example-game

# 5. 验证：漂移检查、落盘即见、溯源
python share_workspace.py --check --workspace example-game
python share_workspace.py --probe --workspace example-game
python share_workspace.py --why .codex/skills --workspace example-game

# 6. 把规则块分发到各客户端规则文件
powershell -NoProfile -ExecutionPolicy Bypass -File sync-agent-memory.ps1 -Update
powershell -NoProfile -ExecutionPolicy Bypass -File sync-agent-memory.ps1 -Check
```

所有写操作都幂等；`--check` 退出码 1 表示漂移，可直接放进 CI 或 pre-commit。

## 文件清单

| 文件 | 职责 |
|---|---|
| `awb_config.py` | 集中配置。路径从 `config.json` 读，支持 `~`、相对路径、`<config_dir>` 占位符；`AWB_CONFIG` 环境变量可覆盖配置文件位置 |
| `config.example.json` | 配置模板（提交） |
| `configs/skills-workspaces.json` | **技能分配表**：`global` 全局技能、`bundles` 技能组、`rules` 按工作空间名正则（第一条命中生效） |
| `configs/agent-skill-paths.json` | 各客户端技能分层路径、优先级方向、是否支持工作空间隔离 |
| `configs/agent-memory-snippet.md` | 规则块源：`win-encoding-rules` / `memory-routing` / `skills-scope` / `shared-workspace`。示例里工具路径按本机 `<AWB_ROOT>\` 写的，部署时改成你的仓库目录 |
| `scope_skills.py` | 按分配表生成链接：全局级写用户级目录，专属级写各工作空间项目级目录 |
| `share_workspace.py` | 建 canonical 共享层、把 7 家客户端目录改成 Junction、`--check` / `--why` / `--probe` / `--snapshot` / `--restore` |
| `cross_agent_skills.py` | 把分配表投影到各客户端项目级 skills 目录（默认 dry-run，`--apply` 才写），不动任何用户级池 |
| `skill_restore.py` | 取消隔离：把保管库全量放回用户级目录 |
| `sync-agent-memory.ps1` | 把 snippet 里的 `<!-- block -->…<!-- /block -->` 逐块同步到各客户端规则文件，`-DryRun` / `-Check` / `-Lint` / `-Status` / `-Update` |

## 命令参考

### scope_skills.py

```bash
python scope_skills.py --status                       # 全局清单 + 各工作空间专属清单
python scope_skills.py --apply                       # 全局 + 全部工作空间重排（幂等）
python scope_skills.py --apply --workspace example-game      # 只重排一个
python scope_skills.py --apply --all                 # 所有工作空间用全量
python scope_skills.py --user-restore                # 取消隔离，全量回用户级
python scope_skills.py --clean-agents                # 清掉多余的跨 agent 临时目录
```

分配表 `rules` 示例（第一条命中生效）：

```json
{ "match": "^(example-game|example-game-.*)$", "bundles": ["game"] }
{ "match": "(example-hiring|hiring|resume)", "bundles": ["hiring", "jobsearch"] }
{ "match": "^example-ops$", "bundles": ["ops"] }
```

未命中任何规则的工作空间只有 `global` 里的技能。

### share_workspace.py

```bash
python share_workspace.py --workspace example-game            # 建 / 刷新共享层
python share_workspace.py --all                       # 所有有专属技能的工作空间
python share_workspace.py --status example-game               # 查看各目录是不是 Junction、指向哪里
python share_workspace.py --check --workspace example-game    # 漂移检查，退出码 1 = 有问题
python share_workspace.py --probe --workspace example-game    # 在 canonical 写探针，验证 8 个 agent 路径都可见
python share_workspace.py --why .codex/skills --workspace example-game   # 溯源到 canonical
python share_workspace.py --snapshot --workspace example-game # 清单快照
python share_workspace.py --restore <snapshot.json> --workspace example-game  # 差异报告 + 重建链接（不删实体）
```

`--check` 检查：7 家 skills 目录 + `.workbuddy/memory` 都是链接且 `realpath` 指向同一 canonical、技能集合与分配表一致、`MEMORY.md` 存在。

### cross_agent_skills.py / skill_restore.py

```bash
python cross_agent_skills.py                         # dry-run 预览（不写）
python cross_agent_skills.py --apply                 # 把专属技能铺到各家项目级目录
python cross_agent_skills.py --apply --workspace example-game
python skill_restore.py                              # 全量回用户级，取消隔离
```

### sync-agent-memory.ps1

```powershell
powershell -NoProfile -File sync-agent-memory.ps1 -Status    # 一致性摘要
powershell -NoProfile -File sync-agent-memory.ps1 -Check     # 只读，drift 则 exit 1
powershell -NoProfile -File sync-agent-memory.ps1 -Lint      # 检查块长度 / 密钥 / 重复行
powershell -NoProfile -File sync-agent-memory.ps1 -DryRun -Update  # 预览
powershell -NoProfile -File sync-agent-memory.ps1 -Update    # 同步，改动文件自动 .bak
```

`-Snippet` 可指定别的块源文件；`-ExtraTargets` 可追加目标文件。

## 支持的客户端

| 客户端 | 全局级 | 项目级 | 同名优先级 | 工作空间隔离 |
|---|---|---|---|---|
| WorkBuddy / CodeBuddy | `~/.workbuddy/skills/` | `.codebuddy/skills/`、`.workbuddy/skills/` | 项目级覆盖用户级 | 有 |
| Claude Code | `~/.claude/skills/` | `.claude/skills/`（支持嵌套子目录） | 用户级覆盖项目级 | 有 |
| Codex | `~/.agents/skills/`、`~/.codex/skills/` | `.agents/skills/`（cwd 向上到 repo root）、`.codex/skills/` | 按 scope 发现 | 有 |
| Qoder | `~/.qoder/skills/` | `.qoder/skills/`（另有 `.qoder/rules/`） | 用户级覆盖项目级 | 有 |
| Trae / Trae CN | `~/.trae-cn/skills` + `user_rules` | `.trae/rules/`（4 种生效方式）；技能项目级路径未文档化 | 规则分层明确 | 部分 |
| DSH (DeepSeek Harness) | `$DSH_HOME/skills/` + `prompt-inject.md` | 无技能项目级；靠 `/rules use` 切规则集 + workspace 基线 | 每轮注入顺序 | 无 |

**优先级方向要注意**：WorkBuddy/CodeBuddy 是项目级先扫优先，Claude 与 Qoder 是用户级覆盖项目级。同一份白名单搬到这两家时，同名技能会由全局池赢。

## 记忆共享的约定

共享层只放**工作空间的知识与进度**，各客户端的私有会话记忆（`~/.codex/memories_v2`、`~/.claude/projects`、`~/.trae-cn/memory`、`~/.dsh/sessions`）保持原样，不并入。规则块里写明了几条协议：

- 入口文件保持 **thin shim**：`CLAUDE.md` / `GEMINI.md` / `.cursor/rules` 只放一行指向 `AGENTS.md` 的指针，不复制协议正文
- **Memory Freshness**：canonical 记忆是摘要与导航层；改代码 / 规划 / 报状态前先核活文件与 git，记忆与现实冲突时以活工作空间为准并回写修正
- **每条事实一个 owner 文档**，ownership 表放工作空间 `AGENTS.md`
- 入口文件保持精简（Codex 读 `AGENTS.md` 默认上限 32 KiB，Claude 建议 `CLAUDE.md` 约 200 行以内），超出下沉到分册或嵌套 `AGENTS.md`
- canonical 里带 `GENERATED` 头的文件是生成产物，改内容请改源规格

## 回滚

- 所有改动都是链接与生成文件；`share_workspace.py` 会把被替换的 `.workbuddy/memory` 备份成 `memory.bak-<时间戳>`
- `sync-agent-memory.ps1 -Update` 每次改动前自动生成 `.bak-<时间戳>`
- `skill_restore.py` 一键取消技能隔离
- `share_workspace.py --restore <snapshot.json>` 报告技能集合差异并重建链接（不删实体文件）

## 与同类项目的关系

思路和以下项目同源，AWB 只吸收做法、不引入依赖：

- [Chemaclass/agnostic-ai](https://github.com/Chemaclass/agnostic-ai) — `sync --check` 漂移闸门、`why` 溯源、provenance markers
- [dyoshikawa/rulesync](https://github.com/dyoshikawa/rulesync) — 单一源 + 反向 import
- [mr-kelly-skills/agent-rules](https://lobehub.com/skills/mr-kelly-skills-agent-rules) — 两个 canonical，其余全链接
- [ucsandman/Agnostic-AI](https://github.com/ucsandman/Agnostic-AI) — skills linked not copied
- [vltansky/sync-agents](https://github.com/vltansky/sync-agents) / [NikashPrakash/dot-agents](https://github.com/NikashPrakash/dot-agents) — 快照、doctor、hard link 坑
- [JonnesLin/workspace-memory-skill](https://github.com/JonnesLin/workspace-memory-skill) / [Omggdavidd/openloops](https://github.com/Omggdavidd/openloops) — thin shim、freshness、ownership、入口精简上限
- [skill.md](https://skill.md/) / [agents.md](https://agents.md/) — Agent Skills 与 AGENTS.md 开放约定

AWB 多出来的部分：**按工作空间名正则分配技能集合**、**16 通道规则同步**、**把宿主会话记忆目录也接进 canonical**。

## 已知边界

- `mk_junction` 在 Windows 用 `mklink /J`；类 Unix 上 `os.symlink` 成功即走 symlink 路径
- 各客户端的**用户级**技能池保持原样；若某客户端用户级已有全量技能，项目级追加只是同名优先，要达到「只剩 6 个」需各自把用户级池也裁成全局集
- 不提供实时协同、文件监听、云端同步；要求只是「落盘后另一边新会话能看到」
- 技能内容（各技能目录里的 `SKILL.md`）本身由真实技能目录共享，本工具只管**集合**与**可见性**

## 许可

MIT — 见 [LICENSE](LICENSE)。
