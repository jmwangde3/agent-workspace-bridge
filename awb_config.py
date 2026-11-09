# -*- coding: utf-8 -*-
"""AWB 集中配置：所有路径一处定义，脚本不再硬编码本机绝对路径。

优先级：环境变量 AWB_CONFIG 指定的文件 > 脚本同目录 config.json > 内置默认值。
路径支持 ~ 展开、相对路径（相对本文件所在目录）、<config_dir> 占位符。
仓库提交 config.example.json；本机 config.json 由 .gitignore 忽略。
"""
import os
import json

HERE = os.path.dirname(os.path.abspath(__file__))

DEFAULTS = {
    "workspace_root": "~/workspaces",
    "config_dir": "configs",
    "skills_vault": "~/.workbuddy/skills-vault",
    "skills_user": "~/.workbuddy/skills",
    "skills_real": "~/.agent_skills/skills",
    "snippet": "~/.workbuddy/agent-memory-snippet.md",
    "skills_workspaces": "<config_dir>/skills-workspaces.json",
    "skills_matrix": "<config_dir>/skills-matrix.json",
    "agent_skill_paths": "<config_dir>/agent-skill-paths.json",
}


def _abs(value, raw):
    if not isinstance(value, str):
        return value
    value = value.replace("<config_dir>", raw["config_dir"])
    value = os.path.expanduser(value)
    if not os.path.isabs(value):
        value = os.path.join(HERE, value)
    return os.path.normpath(value)


def _posix(win_path):
    drive, rest = os.path.splitdrive(win_path)
    return "/" + drive.rstrip(":").lower() + rest.replace("\\", "/")


def load():
    src = os.environ.get("AWB_CONFIG") or os.path.join(HERE, "config.json")
    raw = dict(DEFAULTS)
    if os.path.isfile(src):
        with open(src, encoding="utf-8") as f:
            raw.update(json.load(f))
    raw["config_dir"] = _abs(raw["config_dir"], raw)
    return {k: _abs(v, raw) for k, v in raw.items()}


C = load()
WORKSPACE_ROOT = C["workspace_root"]
CONFIG_DIR = C["config_dir"]
VAULT = C["skills_vault"]
USER = C["skills_user"]
REAL = C["skills_real"]
SNIPPET = C["snippet"]
CFG = C["skills_workspaces"]
MATRIX = C["skills_matrix"]
AGENT_PATHS = C["agent_skill_paths"]
POSIX_REAL = _posix(REAL)
