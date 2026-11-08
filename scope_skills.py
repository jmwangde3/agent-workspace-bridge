# -*- coding: utf-8 -*-
"""按 WorkBuddy 工作空间分配技能（本机专用）。

宿主扫描顺序（shared.cjs / SkillProductProvider）：
    workspace  {cwd}/.codebuddy/skills/   ← cwd = 当前工作空间根，天生按工作空间隔离
    user       ~/.workbuddy/skills/        ← 全局级，所有工作空间可见
    connector  ~/.workbuddy/connectors/skills/
    builtin    resources/plugins/workbuddy-builtin/skills/

按技能名去重、先扫到的优先。所以：
    全局技能放 ~/.workbuddy/skills/
    工作空间专属只放 <workspace>/.codebuddy/skills/（以及 .workbuddy/skills/ 双写）
→ A 工作空间看不到 B 的专属技能。

分配表：skills-workspaces.json（global + bundles + rules，第一条命中生效）。
保管库 ~/.workbuddy/skills-vault/ 存全部 18 个链接，宿主不扫描。

用法：
    python scope_skills.py --status
    python scope_skills.py --apply                       # 全局 + 全部工作空间重排
    python scope_skills.py --apply --workspace "<path>" # 只重排一个工作空间
    python scope_skills.py --clean-agents               # 清掉上一轮多余的其他 agent 目录
    python scope_skills.py --user-restore               # 取消隔离，全量回用户级
"""
import os
import re
import sys
import json
import shutil
import argparse
import subprocess

from awb_config import CFG, VAULT, USER, REAL, POSIX_REAL, WORKSPACE_ROOT
REPARSE_POINT = 0x400

# 上一轮铺的、与工作空间语义无关的其他 agent 项目目录
EXTRA_AGENT_DIRS = [".claude/skills", ".codex/skills", ".agents/skills", ".gemini/skills"]


def load_cfg():
    with open(CFG, encoding="utf-8") as f:
        return json.load(f)


def vault_names():
    if not os.path.isdir(VAULT):
        return []
    return sorted(n for n in os.listdir(VAULT) if not n.startswith("."))


def is_link_or_junction(p):
    if os.path.islink(p):
        return True
    try:
        return bool(getattr(os.lstat(p), "st_file_attributes", 0) & REPARSE_POINT)
    except OSError:
        return False


def clear_links(dst):
    for n in os.listdir(dst):
        p = os.path.join(dst, n)
        if is_link_or_junction(p):
            if os.path.islink(p):
                os.unlink(p)
            else:
                os.rmdir(p)  # Junction：只删链接，不碰目标


def make_link(target, out):
    try:
        os.symlink(target, out, target_is_directory=True)
        return None
    except OSError as e1:
        r = subprocess.run(["cmd", "/c", "mklink", "/J", out, target],
                           capture_output=True, text=True, encoding="gbk", errors="replace")
        if r.returncode == 0:
            return None
        return "%s | mklink rc=%s %s" % (e1, r.returncode, (r.stdout + r.stderr).strip()[:160])


def link_into(names, dst):
    os.makedirs(dst, exist_ok=True)
    clear_links(dst)
    made, failed = [], []
    for n in names:
        out = os.path.join(dst, n)
        if os.path.lexists(out):
            continue
        err = make_link(os.path.join(REAL, n), out)
        if err:
            failed.append((n, err))
        else:
            made.append(n)
    return made, failed


def bundles_for(name, cfg):
    """第一条命中的规则生效；返回 (技能名列表, 命中的规则)。"""
    for r in cfg.get("rules", []):
        if re.search(r["match"], name):
            out = []
            for b in r.get("bundles", []):
                out.extend(cfg["bundles"].get(b, []))
            return out, r["match"]
    return [], None


def report_fail(failed, dst):
    for n, e in failed:
        print("  FAILED", n, e, "| ln -s \"%s/%s\" \"%s\"" % (POSIX_REAL, n, os.path.join(dst, n)))


def status(cfg):
    pool = vault_names()
    print("=== global (user-level, all workspaces) ===")
    d = USER
    items = sorted(os.listdir(d)) if os.path.isdir(d) else []
    print(" ", [n for n in items if is_link_or_junction(os.path.join(d, n))])
    print("=== workspace-specific ===")
    root = WORKSPACE_ROOT
    for name in sorted(os.listdir(root)):
        p = os.path.join(root, name)
        if not os.path.isdir(p):
            continue
        d = os.path.join(p, ".codebuddy", "skills")
        if os.path.isdir(d):
            links = sorted(n for n in os.listdir(d) if is_link_or_junction(os.path.join(d, n)))
            if links:
                print(" %-28s %s" % (name, links))
    print("=== vault ===")
    print(" ", len(pool), "items ->", VAULT)


def apply(cfg, only=None):
    pool = vault_names()
    glob = [n for n in cfg["global"] if n in pool]
    made, failed = link_into(glob, USER)
    print("global -> %s : %d links %s" % (USER, len(made), glob))
    report_fail(failed, USER)

    root = WORKSPACE_ROOT
    hit = 0
    for name in sorted(os.listdir(root)):
        p = os.path.join(root, name)
        if not os.path.isdir(p):
            continue
        if only and os.path.basename(os.path.normpath(only)) != name:
            continue
        names, rule = bundles_for(name, cfg)
        if not names:
            continue
        names = [n for n in names if n in pool]
        for rel in cfg.get("project_skill_dirs", [".codebuddy/skills"]):
            dst = os.path.join(p, *rel.split("/"))
            made, failed = link_into(names, dst)
            print(" %-28s rule=%-24s linked=%d %s" % (name, rule, len(made), names))
            report_fail(failed, dst)
        hit += 1
    print("workspaces with dedicated skills =", hit)


def clean_agents(cfg, workspace=None):
    p = workspace or os.getcwd()
    for rel in EXTRA_AGENT_DIRS:
        d = os.path.join(p, *rel.split("/"))
        if not os.path.isdir(d):
            continue
        for n in os.listdir(d):
            q = os.path.join(d, n)
            if is_link_or_junction(q):
                if os.path.islink(q):
                    os.unlink(q)
                else:
                    os.rmdir(q)
        try:
            os.rmdir(d)
            print("removed", d)
        except OSError as e:
            print("kept (not empty):", d, e)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--workspace")
    ap.add_argument("--clean-agents", action="store_true")
    ap.add_argument("--user-restore", action="store_true")
    a = ap.parse_args()
    cfg = load_cfg()

    if a.user_restore:
        names = vault_names()
        made, failed = link_into(names, USER)
        print("user-restore: linked", len(made), "->", USER)
        report_fail(failed, USER)
        return

    if a.clean_agents:
        return clean_agents(cfg, a.workspace or os.getcwd())

    if a.apply:
        return apply(cfg, a.workspace)

    return status(cfg)


if __name__ == "__main__":
    main()
