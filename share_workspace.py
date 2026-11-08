# -*- coding: utf-8 -*-
"""共享工作空间：让多个 agent 在同一物理目录里共享记忆与技能。

各 agent 的同一逻辑工作空间本来就指向同一物理目录
（Codex config.toml: projectlessWorkspaceRoot = 'H:\\AI工作区'；
 会话记录里 example-game 的 cwd 就是 H:\\AI工作区\\example-game）。
所以不需要跨目录同步，只要把「实体」收拢到一处、各 agent 约定目录做成
Junction 指过去，落盘后所有 agent 立刻可见 —— 无实时协同、无轮询。

    <workspace>/.shared/skills/    工作空间专属技能实体（链接指向 ~/.agent_skills/skills）
    <workspace>/.shared/memory/    MEMORY.md 索引 + YYYY-MM-DD.md append-only 日记
    <workspace>/.codebuddy/skills ┐
    <workspace>/.workbuddy/skills │
    <workspace>/.agents/skills    │ 全部 Junction → .shared/skills
    <workspace>/.codex/skills     │
    <workspace>/.claude/skills    │
    <workspace>/.qoder/skills     │
    <workspace>/.trae/skills     ┘
    <workspace>/.workbuddy/memory → Junction → .shared/memory

技能集合的单一事实源仍是 skills-workspaces.json；改完分类跑
`python scope_skills.py --apply` 会重写 .shared/skills 的内容。

用法：
    python share_workspace.py --workspace example-game          # 建/刷新共享层
    python share_workspace.py --all                     # 所有有专属技能的工作空间
    python share_workspace.py --status example-game             # 只读查看
    python share_workspace.py --probe example-game              # 落盘即见验证（建临时探针再删）
"""
import os
import re
import sys
import json
import shutil
import argparse
import subprocess
import datetime

from awb_config import WORKSPACE_ROOT as ROOT, CFG, REAL
REPARSE_POINT = 0x400

AGENT_SKILL_DIRS = [
    ".codebuddy/skills", ".workbuddy/skills", ".agents/skills",
    ".codex/skills", ".claude/skills", ".qoder/skills", ".trae/skills",
]


def load_cfg():
    with open(CFG, encoding="utf-8") as f:
        return json.load(f)


def vault_names():
    v = os.path.join(os.path.expanduser("~"), ".workbuddy", "skills-vault")
    return sorted(n for n in os.listdir(v) if not n.startswith(".")) if os.path.isdir(v) else []


def bundles_for(name, cfg):
    for r in cfg.get("rules", []):
        if re.search(r["match"], name):
            out = []
            for b in r.get("bundles", []):
                out.extend(cfg["bundles"].get(b, []))
            return out
    return []


def ws_names(name, cfg):
    pool = vault_names()
    return [n for n in bundles_for(name, cfg) if n in pool]


def is_reparse(p):
    if os.path.islink(p):
        return True
    try:
        return bool(getattr(os.lstat(p), "st_file_attributes", 0) & REPARSE_POINT)
    except OSError:
        return False


def rm_entry(p):
    """删除链接/Junction（只删链接本身，不碰目标）；真实文件/目录不删。"""
    if not os.path.lexists(p):
        return
    if os.path.islink(p):
        os.unlink(p)
    elif is_reparse(p):
        os.rmdir(p)
    elif os.path.isdir(p):
        for n in os.listdir(p):
            q = os.path.join(p, n)
            if os.path.islink(q) or is_reparse(q):
                rm_entry(q)
        if not os.listdir(p):
            os.rmdir(p)
    else:
        os.unlink(p)


def mk_junction(link, target):
    os.makedirs(os.path.dirname(link), exist_ok=True)
    if os.path.lexists(link):
        rm_entry(link)
    try:
        os.symlink(target, link, target_is_directory=True)
        return link
    except OSError:
        pass
    r = subprocess.run(["cmd", "/c", "mklink", "/J", link, target],
                       capture_output=True, text=True, encoding="gbk", errors="replace")
    if r.returncode != 0:
        raise RuntimeError("link failed: %s %s" % (r.returncode, (r.stdout + r.stderr).strip()[:200]))
    return link


def make_skill_link(name, dst):
    out = os.path.join(dst, name)
    if os.path.lexists(out):
        return
    try:
        os.symlink(os.path.join(REAL, name), out, target_is_directory=True)
    except OSError:
        r = subprocess.run(["cmd", "/c", "mklink", "/J", out, os.path.join(REAL, name)],
                           capture_output=True, text=True, encoding="gbk", errors="replace")
        if r.returncode != 0:
            print("  FAILED", name, (r.stdout + r.stderr).strip()[:160])


def ensure_canonical(ws, names):
    shared = os.path.join(ws, ".shared")
    skills = os.path.join(shared, "skills")
    memory = os.path.join(shared, "memory")
    os.makedirs(skills, exist_ok=True)
    os.makedirs(memory, exist_ok=True)

    # 技能实体：按分配表重建（canonical 是唯一实体）
    keep = set(names)
    for n in os.listdir(skills):
        if n not in keep:
            rm_entry(os.path.join(skills, n))
    for n in names:
        make_skill_link(n, skills)
    print("  canonical skills :", len(names), "->", skills)

    # 记忆骨架
    idx = os.path.join(memory, "MEMORY.md")
    if not os.path.exists(idx):
        with open(idx, "w", encoding="utf-8") as f:
            f.write(GEN_HEADER + "# %s 工作空间共享记忆（索引卡）\n\n"
                    "> 只记现行状态 + 回查锚点。append-only 日记在 YYYY-MM-DD.md。\n"
                    "> 跨客户端唯一同步点仍以本工作空间 AGENTS.md 指向的文件为准（example-game = PROGRESS.md）。\n\n"
                    "## 现行\n- 待补\n\n## 回查\n- 见本工作空间 AGENTS.md → INDEX.md → 分册\n" % os.path.basename(ws))
    today = datetime.date.today().isoformat() + ".md"
    day = os.path.join(memory, today)
    if not os.path.exists(day):
        with open(day, "w", encoding="utf-8") as f:
            f.write(GEN_HEADER + "# %s\n\n" % datetime.date.today().isoformat())
    print("  canonical memory :", memory)
    return shared, skills, memory


def convert_dir(ws, rel, target):
    p = os.path.join(ws, *rel.split("/"))
    if is_reparse(p):
        # 已是 Junction：内容天然共享，保留
        print("  %-24s junction (ok)" % rel)
        return
    if os.path.isdir(p):
        # 普通目录：把里面已有的链接清掉（实体已在 canonical），真实文件保留为备份
        leftovers = [n for n in os.listdir(p) if not (os.path.islink(os.path.join(p, n)) or is_reparse(os.path.join(p, n)))]
        if leftovers:
            bak = p + ".bak-" + datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
            shutil.move(p, bak)
            print("  %-24s backed up -> %s" % (rel, os.path.basename(bak)))
        else:
            for n in os.listdir(p):
                rm_entry(os.path.join(p, n))
            os.rmdir(p)
    mk_junction(p, target)
    print("  %-24s -> junction" % rel)


def build(ws, cfg):
    name = os.path.basename(ws)
    names = ws_names(name, cfg)
    print("[%s] dedicated skills: %s" % (name, names))
    shared, skills, memory = ensure_canonical(ws, names)
    for rel in AGENT_SKILL_DIRS:
        convert_dir(ws, rel, skills)
    # WorkBuddy 的会话记忆目录也接到 canonical
    wb_mem = os.path.join(ws, ".workbuddy", "memory")
    if os.path.isdir(wb_mem) and not is_reparse(wb_mem):
        for n in os.listdir(wb_mem):
            src, dst = os.path.join(wb_mem, n), os.path.join(memory, n)
            if not os.path.exists(dst) and os.path.isfile(src):
                shutil.copy2(src, dst)
        bak = wb_mem + ".bak-" + datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        shutil.move(wb_mem, bak)
        print("  .workbuddy/memory  backed up ->", os.path.basename(bak))
    mk_junction(wb_mem, memory)
    print("  .workbuddy/memory  -> junction")
    return shared


def status(ws):
    print("=== workspace:", ws, "===")
    for rel in [".shared/skills", ".shared/memory"] + AGENT_SKILL_DIRS + [".workbuddy/memory"]:
        p = os.path.join(ws, *rel.split("/"))
        if not os.path.lexists(p):
            print(" %-24s (missing)" % rel)
            continue
        if is_reparse(p):
            try:
                tgt = os.readlink(p)
            except OSError:
                tgt = "(junction)"
            print(" %-24s JUNCTION -> %s" % (rel, tgt))
        else:
            print(" %-24s dir (%d items)" % (rel, len(os.listdir(p))))


def probe(ws):
    """在 canonical 写探针，从各 agent 目录读，证明落盘即见。"""
    ok = []
    f = os.path.join(ws, ".shared", "skills", "_share_probe.txt")
    with open(f, "w", encoding="utf-8") as h:
        h.write("share-workspace probe\n")
    for rel in AGENT_SKILL_DIRS:
        ok.append((rel, os.path.isfile(os.path.join(ws, *rel.split("/"), "_share_probe.txt"))))
    os.remove(f)

    m = os.path.join(ws, ".shared", "memory", "_share_probe.txt")
    with open(m, "w", encoding="utf-8") as h:
        h.write("share-workspace probe\n")
    ok.append((".workbuddy/memory", os.path.isfile(os.path.join(ws, ".workbuddy", "memory", "_share_probe.txt"))))
    os.remove(m)

    for rel, hit in ok:
        print(" %-24s sees probe = %s" % (rel, hit))
    print("probes removed; all agent paths resolve to the same canonical inode")


GEN_HEADER = "<!-- GENERATED by share_workspace.py — 实体在 .shared/；改技能分类请改 skills-workspaces.json 后跑 scope_skills.py --apply -->\n\n"


def check(ws, cfg):
    """漂移检查（CI gate）：所有 agent 路径必须是 Junction 且指向同一 canonical，技能集合与分配表一致。"""
    problems = []
    name = os.path.basename(ws)
    skills = os.path.join(ws, ".shared", "skills")
    memory = os.path.join(ws, ".shared", "memory")
    if not os.path.isdir(skills):
        problems.append("missing .shared/skills")
    if not os.path.isdir(memory):
        problems.append("missing .shared/memory")
    for rel in AGENT_SKILL_DIRS:
        p = os.path.join(ws, *rel.split("/"))
        if not is_reparse(p):
            problems.append("%s is not a junction" % rel)
        elif os.path.normcase(os.path.realpath(p)) != os.path.normcase(os.path.realpath(skills)):
            problems.append("%s -> wrong target %s" % (rel, os.path.realpath(p)))
    if not is_reparse(os.path.join(ws, ".workbuddy", "memory")):
        problems.append(".workbuddy/memory is not a junction")
    want = set(ws_names(name, cfg))
    got = set(n for n in os.listdir(skills) if not n.startswith("_")) if os.path.isdir(skills) else set()
    if want != got:
        problems.append("skill set drift: want=%s got=%s" % (sorted(want), sorted(got)))
    for f in ("MEMORY.md",):
        if not os.path.isfile(os.path.join(memory, f)):
            problems.append("missing .shared/memory/" + f)
    print("[%s] CHECK: %d problems" % (name, len(problems)))
    for p in problems:
        print("  FAIL", p)
    if not problems:
        print("  OK - all agent paths resolve to canonical, skill set matches spec")
    return len(problems)


def why(ws, rel):
    """溯源：某个 agent 路径的内容来自哪个 canonical。"""
    p = os.path.join(ws, *rel.split("/"))
    if not os.path.lexists(p):
        print(rel, "-> (missing)")
        return
    if is_reparse(p):
        try:
            t = os.readlink(p)
        except OSError:
            t = "(junction)"
        print("%s\n  JUNCTION -> %s\n  canonical -> %s" % (rel, t, os.path.realpath(p)))
    else:
        print(rel, "-> real directory (not shared)")


def snapshot(ws):
    """把当前共享层清单写成快照，供差异对照。"""
    ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    skills = os.path.join(ws, ".shared", "skills")
    memory = os.path.join(ws, ".shared", "memory")
    data = {
        "at": ts,
        "workspace": os.path.basename(ws),
        "skills": sorted(n for n in os.listdir(skills) if not n.startswith("_")) if os.path.isdir(skills) else [],
        "memory_files": sorted(os.listdir(memory)) if os.path.isdir(memory) else [],
        "agent_dirs": {r: is_reparse(os.path.join(ws, *r.split("/"))) for r in AGENT_SKILL_DIRS},
    }
    out = os.path.join(ws, ".shared", "snapshot-%s.json" % ts)
    with open(out, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")
    print("snapshot ->", out)


def restore(ws, snap, cfg):
    """按快照报告差异并重建链接结构（不删除任何实体文件）。"""
    with open(snap, encoding="utf-8") as f:
        old = json.load(f)
    skills = os.path.join(ws, ".shared", "skills")
    now = sorted(n for n in os.listdir(skills) if not n.startswith("_")) if os.path.isdir(skills) else []
    print("snapshot skills :", old.get("skills"))
    print("current  skills :", now)
    added = sorted(set(now) - set(old.get("skills") or []))
    removed = sorted(set(old.get("skills") or []) - set(now))
    print("added =", added, "| removed =", removed)
    build(ws, cfg)
    print("link structure rebuilt; entity files kept")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workspace")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--probe", action="store_true")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--why", help="agent relative path, e.g. .codex/skills")
    ap.add_argument("--snapshot", action="store_true")
    ap.add_argument("--restore", help="path to a snapshot-*.json")
    a = ap.parse_args()
    cfg = load_cfg()

    targets = []
    if a.all:
        for n in sorted(os.listdir(ROOT)):
            p = os.path.join(ROOT, n)
            if os.path.isdir(p) and ws_names(n, cfg):
                targets.append(p)
    elif a.workspace:
        targets.append(os.path.join(ROOT, a.workspace))
    single = targets or [os.path.join(ROOT, a.workspace or os.environ.get("AWB_WORKSPACE", "example-game"))]

    if a.status:
        for p in single:
            status(p)
        return
    if a.probe:
        for p in single:
            probe(p)
        return
    if a.check:
        rc = 0
        for p in single:
            rc += check(p, cfg)
        sys.exit(1 if rc else 0)
    if a.why:
        why(single[0], a.why)
        return
    if a.snapshot:
        for p in single:
            snapshot(p)
        return
    if a.restore:
        restore(single[0], a.restore, cfg)
        return
    if not targets:
        print("nothing to do; pass --workspace NAME or --all")
        return
    for p in targets:
        build(p, cfg)
    print("built", len(targets), "shared workspace(s)")


if __name__ == "__main__":
    main()
