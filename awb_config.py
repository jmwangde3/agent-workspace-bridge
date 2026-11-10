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


class ConfigError(ValueError):
    pass


def _abs(value):
    value = os.path.expanduser(value)
    if value.startswith("~") or "\0" in value:
        raise ConfigError("Unresolved or invalid configuration path")
    if not os.path.isabs(value):
        value = os.path.join(HERE, value)
    return os.path.normpath(value)



def config_source():
    if "AWB_CONFIG" in os.environ:
        path = os.environ["AWB_CONFIG"]
        if not path.strip():
            raise ConfigError("AWB_CONFIG is empty or whitespace")
        return os.path.abspath(_abs_from_cwd(path))
    portable = os.path.join(HERE, "config.json")
    user = os.path.join(DATA_DIR, "config.json")
    return next((path for path in (portable, user) if os.path.isfile(path)), None)


def _abs_from_cwd(path):
    path = os.path.expanduser(path)
    if path.startswith("~") or "\0" in path:
        raise ConfigError("AWB_CONFIG contains an unresolved or invalid path")
    return path


def load():
    src = config_source()
    raw = dict(DEFAULTS)
    if src is not None:
        try:
            if os.path.isdir(src):
                raise ConfigError("Configuration is a directory: " + src)
            with open(src, encoding="utf-8-sig") as f:
                values = json.load(f)
        except json.JSONDecodeError as error:
            raise ConfigError(f"Invalid JSON in {src}, line {error.lineno}, column {error.colno}") from error
        except FileNotFoundError as error:
            raise ConfigError("Configuration file does not exist: " + src) from error
        except (OSError, UnicodeError) as error:
            raise ConfigError(f"Cannot read configuration {src}: {error.__class__.__name__}") from error
        if not isinstance(values, dict):
            raise ConfigError("Configuration must be a JSON object: " + src)
        raw.update({key: value for key, value in values.items() if key in DEFAULTS})
    for key, value in raw.items():
        if not isinstance(value, str) or not value.strip():
            raise ConfigError(f"{key} must be a nonempty string in {src}")
    if "<config_dir>" in raw["config_dir"]:
        raise ConfigError(f"config_dir cannot reference itself in {src}")
    config_dir = _abs(raw["config_dir"])
    return {key: _abs(value.replace("<config_dir>", config_dir)) for key, value in raw.items()}


try:
    SOURCE = config_source()
    C = load()
    ERROR = None
except ConfigError as error:
    SOURCE, C, ERROR = None, {}, error
WORKSPACE_ROOT = C.get("workspace_root")
CONFIG_DIR = C.get("config_dir")
SNIPPET = C.get("snippet")


def require_config():
    if ERROR is not None:
        raise ERROR


def list_workspaces():
    require_config()
    if not os.path.isdir(WORKSPACE_ROOT):
        return []
    return sorted(name for name in os.listdir(WORKSPACE_ROOT)
                  if not name.startswith(".") and os.path.isdir(os.path.join(WORKSPACE_ROOT, name)))
