# -*- coding: utf-8 -*-
"""Policy Engine 独立验证一键入口。

依次执行：
  1) 120 条主集（冻结金标，回归用）
  2) 40 条独立留出集（未参与规则开发）
  3) 208 个变异体（检验测试集本身有没有鉴别力）
  4) 17 条作用域/优先级负样本（由变异测试驱动补充，已并入 3 的判定语料）

DB 侧另有 scripts/verify_policy_rules.py（需 docker compose 已启动）。

用法：python tests/eval/run_policy_validation.py
退出码：0 = 主集与留出集全部通过；1 = 任一失败。
"""
from __future__ import annotations

import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
STEPS = [
    ("主集 120 条", ["tests/eval/run_policy.py"]),
    ("独立留出集 40 条", ["tests/eval/run_policy_holdout.py"]),
    ("变异测试 208 个", ["tests/eval/mutation_policy.py"]),
]


def main() -> int:
    failed = []
    for name, args in STEPS:
        print(f"\n=== {name} ===", flush=True)
        rc = subprocess.call([sys.executable, "-X", "utf8", *args], cwd=str(ROOT))
        if rc != 0:
            failed.append(name)
    print("\n=== 汇总 ===")
    print("全部通过" if not failed else f"失败：{failed}")
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())