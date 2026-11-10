# -*- coding: utf-8 -*-
"""AWB 集中配置：所有路径一处定义，脚本不再硬编码本机绝对路径。

优先级：AWB_CONFIG > 程序旁的 config.json > 用户数据目录的 config.json > 内置默认值。
路径支持 ~ 展开、相对路径（相对本文件所在目录）、<config_dir> 占位符。
仓库提交 config.example.json；本机 config.json 由 .gitignore 忽略。
"""
import os
import json
import sys

HERE = os.path.dirname(sys.executable if getattr(sys, "frozen", False) else os.path.abspath(__file__))
DATA_DIR = os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~/.local/share"), "AWB")

DEFAULTS = {
    "workspace_root": "~/workspaces",
    "config_dir": "configs",
    "snippet": "~/.workbuddy/agent-memory-snippet.md",
}


def _abs(value, raw):
    if not isinstance(value, str):
        return value
    value = value.replace("<config_dir>", raw["config_dir"])
    value = os.path.expanduser(value)
    if not os.path.isabs(value):
        value = os.path.join(HERE, value)
    return os.path.normpath(value)



def load():
    portable = os.path.join(HERE, "config.json")
    src = os.environ.get("AWB_CONFIG") or (
        portable if os.path.isfile(portable) else os.path.join(DATA_DIR, "config.json"))
    raw = dict(DEFAULTS)
    if os.path.isfile(src):
        with open(src, encoding="utf-8") as f:
            raw.update({key: value for key, value in json.load(f).items() if key in DEFAULTS})
    raw["config_dir"] = _abs(raw["config_dir"], raw)
    return {k: _abs(v, raw) for k, v in raw.items()}


C = load()
WORKSPACE_ROOT = C["workspace_root"]
CONFIG_DIR = C["config_dir"]
SNIPPET = C["snippet"]


def list_workspaces():
    if not os.path.isdir(WORKSPACE_ROOT):
        return []
    return sorted(name for name in os.listdir(WORKSPACE_ROOT)
                  if not name.startswith(".") and os.path.isdir(os.path.join(WORKSPACE_ROOT, name)))
