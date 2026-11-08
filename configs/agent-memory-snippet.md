<!-- win-encoding-rules -->
## Windows 中文环境 / 编码规则（跨客户端共用）

环境：Windows PowerShell 5.1 Desktop + zh-CN / 代码页 936(GBK)，未开系统级 UTF-8；Bash 侧 UTF-8 正常。

- 优先用 Bash；必须用 PowerShell 时开头写 `$OutputEncoding = [Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)`（工具会话 -NoProfile，profile 不加载）。
- 读文件 `Get-Content -Raw -Encoding UTF8`；写文件 `[System.IO.File]::WriteAllText($p,$s,(New-Object System.Text.UTF8Encoding($false)))`；禁用裸 `Out-File` / `>` / `Set-Content`。
- `.ps1` 含中文 → 存 UTF-8 带 BOM；`.bat/.cmd` → 存 GBK(936) 无 BOM。
- Python 所有 `open()` / `write_text()` 显式 `encoding="utf-8"`。
- stdout 用 ASCII/英文标签；中文路径一律加引号，PowerShell 用 `-LiteralPath`。
- 命令同时含中文路径与嵌套引号时，把脚本落盘为 UTF-8 with BOM 的 `.ps1` 再 `-File` 执行，或走包装脚本，不直接拼 `-Command`。
- 遇到中文乱码不要反复换命令试错，按上面几条改一次。
<!-- /win-encoding-rules -->

<!-- memory-routing -->
## 记忆入口（文件层 · 跨客户端统一）
- 唯一事实源是磁盘文件。跨项目规则＝用户级 `MEMORY.md`；项目规则＝项目根 `AGENTS.md`；项目事实＝各项目知识库，全部纳入 git。
- 会话开始先读项目 `AGENTS.md` 与知识库索引；命中条目直接引用并说出出处与适用版本，没命中再调查。
- 改完任何文件，在工作空间唯一同步点（如 `PROGRESS.md`）末尾追加 1–3 行，不动别人的行；跨层事实只在一处出现。
- 本块由 `sync-agent-memory.ps1 -Update` 同步到各客户端规则文件，别手改各通道副本。
<!-- /memory-routing -->

<!-- skills-scope -->
## 技能作用域（按工作空间）
- 宿主扫描顺序 workspace `{cwd}/.codebuddy/skills/` → user `~/.workbuddy/skills/` → connector → builtin，按技能名去重、先扫优先。
- 全局级技能放用户级 skills 目录，所有工作空间可见；工作空间专属只放 `<WORKSPACES>/<workspace>/.codebuddy/skills/`，cwd 不同 → 各工作空间天然互相看不到对方的专属技能。
- 分配表 `<AWB_ROOT>/skills-workspaces.json`：global 列表 + bundles + rules（第一条命中生效，未命中者只有全局）。改分类只改这个 JSON，再跑 `python scope_skills.py --apply`。
- 全量技能链接保管在保管库目录（宿主不扫描）；`--status` 查看各工作空间清单，`--user-restore` 取消隔离。
<!-- /skills-scope -->

<!-- shared-workspace -->
## 共享工作空间（多客户端落盘即见）
- 各客户端的同一逻辑工作空间指向同一物理目录，无需跨目录同步。
- canonical 层 `<workspace>/.shared/skills/` 放工作空间专属技能实体，`<workspace>/.shared/memory/` 放 MEMORY.md 索引与 YYYY-MM-DD.md append-only 日记。
- 各客户端项目级 skills 目录与会话记忆目录做成链接指向 canonical；一处增删，落盘后所有客户端立即可见，不要求同时协同。目录链接用 Junction（Windows，`mklink /J`）或 symlink（类 Unix）。
- 记忆写回统一到工作空间 `AGENTS.md` 指定的唯一同步点与 `.shared/memory/`；各客户端私有会话记忆不并入。
- 入口文件保持 thin shim：其他客户端的规则文件只放一行指向 `AGENTS.md` 的指针，不复制协议正文。
- 记忆新鲜度：canonical 记忆是摘要与导航层；改代码/规划/报状态前先核活文件与 git，记忆与现实冲突时以活工作空间为准并回写修正。
- 每条事实只有一个 owner 文档，ownership 表放工作空间 `AGENTS.md`；入口文件保持精简（Codex 读 AGENTS.md 默认上限 32 KiB，Claude 建议 CLAUDE.md 约 200 行以内）。
- canonical 里带 GENERATED 头的文件是生成产物，改内容请改源规格。
- 工具 `<AWB_ROOT>/share_workspace.py`：--workspace / --all / --status / --probe / --check / --why / --snapshot / --restore。
<!-- /shared-workspace -->
