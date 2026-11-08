# -*- coding: utf-8 -*-
"""取消项目级技能隔离：把保管库里的链接放回 ~/.workbuddy/skills/。

用法：python skill_restore.py
用法见 README.md；等价命令 python scope_skills.py --user-restore。
"""
import os
import shutil

from awb_config import VAULT as SRC, USER as DST
NAMES = sorted(n for n in os.listdir(SRC) if os.path.isdir(os.path.join(SRC, n))) if os.path.isdir(SRC) else []


def main():
    for n in NAMES:
        s = os.path.join(SRC, n)
        d = os.path.join(DST, n)
        if os.path.lexists(d):
            print("already present:", n)
        elif os.path.lexists(s):
            shutil.move(s, d)
            print("restored:", n)
        else:
            print("missing in backup:", n)
    print("done")


if __name__ == "__main__":
    main()
