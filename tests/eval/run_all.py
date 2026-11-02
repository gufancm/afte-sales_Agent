# -*- coding: utf-8 -*-
"""评测集总 runner：跑 E6/E9/E1/E28（离线，必跑）；E35 需完整栈，用 --with-uat 显式开启。

用法：
  python tests/eval/run_all.py              # 离线四集
  python tests/eval/run_all.py --with-uat   # 追加端到端 UAT（需 API 在线）
输出：reports/eval-<date>.md（含门槛判定表）
"""
from __future__ import annotations

import argparse
import datetime
import os
import pathlib
import subprocess
import sys

HERE = pathlib.Path(__file__).parent
OUT_DIR = HERE.parents[1] / "reports"
OFFLINE = [
    ("E6 政策决策矩阵", "run_policy.py"),
    ("E9 证据闭包完整性", "run_closure.py"),
    ("E1 意图识别(离线)", "run_intent.py"),
    ("E28 注入对抗(离线)", "run_adversarial.py"),
]


def _run(script: str) -> tuple[int, str, str]:
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env.setdefault("UAT_TIMEOUT", "45")
    p = subprocess.run([sys.executable, str(HERE / script)], capture_output=True,
                       text=True, encoding="utf-8", errors="replace", env=env)
    return p.returncode, (p.stdout or ""), (p.stderr or "")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--with-uat", action="store_true", help="追加 E35 端到端 UAT（需 API 在线）")
    args = ap.parse_args()
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    runners = list(OFFLINE)
    if args.with_uat:
        runners.append(("E35 UAT 场景", "run_uat.py"))
    rows = []
    for name, script in runners:
        rc, stdout, stderr = _run(script)
        last = [l for l in stdout.strip().splitlines() if l.strip()]
        verdict = last[-1] if last else "(no output)"
        rows.append((name, rc == 0, verdict))
        print(f"== {name} ==\n{stdout}{stderr}")
    out = OUT_DIR / ("eval-%s.md" % datetime.date.today().isoformat())
    lines = ["# 评测集运行报告", "", "日期：%s" % datetime.date.today().isoformat(), "",
             "| 评测集 | 结果 | 末行判定 |", "| --- | --- | --- |"]
    for name, ok, verdict in rows:
        lines.append("| %s | %s | `%s` |" % (name, "PASS" if ok else "FAIL", verdict))
    failed = [n for n, ok, _ in rows if not ok]
    lines += ["", ("全部通过。" if not failed else "失败项：" + "、".join(failed))]
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("报告：", out)
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())