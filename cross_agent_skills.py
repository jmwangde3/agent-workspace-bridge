# -*- coding: utf-8 -*-
"""把 WorkBuddy 工作空间技能分配投影到其他 agent 的项目级目录。

默认 dry-run（只打印）；--apply 才创建链接。
只读 skills-workspaces.json 与 agent-skill-paths.json；不动任何 agent 的用户级技能池。

用法：
    python cross_agent_skills.py                       # dry-run 全量预览
    python cross_agent_skills.py --apply               # 真的建链接
    python cross_agent_skills.py --apply --workspace example-game   # 只处理一个工作空间
"""
import os
import json
import argparse
import scope_skills as S

from awb_config import AGENT_PATHS as PATHS, WORKSPACE_ROOT


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--workspace", help="only this workspace basename")
    a = ap.parse_args()

    cfg = S.load_cfg()
    with open(PATHS, encoding="utf-8") as f:
        x = json.load(f)
    dirs = sorted({d for ag in x["agents"] for d in ag.get("project_dirs", [])})
    pool = S.vault_names()

    print("project dirs:", dirs)
    print("mode:", "APPLY" if a.apply else "DRY-RUN (no writes)")
    root = WORKSPACE_ROOT
    n_ws = 0
    for name in sorted(os.listdir(root)):
        p = os.path.join(root, name)
        if not os.path.isdir(p):
            continue
        if a.workspace and name != a.workspace:
            continue
        names, rule = S.bundles_for(name, cfg)
        if not names:
            continue
        names = [n for n in names if n in pool]
        n_ws += 1
        for rel in dirs:
            dst = os.path.join(p, *rel.split("/"))
            if a.apply:
                made, failed = S.link_into(names, dst)
                print(" %-28s %-20s linked=%d" % (name, rel, len(made)))
                for n, e in failed:
                    print("   FAILED", n, e)
            else:
                print(" %-28s %-20s would link=%d %s" % (name, rel, len(names), names))
    print("workspaces =", n_ws, "| link targets =", n_ws * len(dirs))


if __name__ == "__main__":
    main()
