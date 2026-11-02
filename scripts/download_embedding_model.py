# -*- coding: utf-8 -*-
"""把 Hugging Face 上的 embedding/reranker 权重下载到本地目录（离线/内网可复现部署）。

国内 huggingface.co 常不可达，默认改用 hf-mirror.com（可用 HF_ENDPOINT 覆盖）。

用法：
  python scripts/download_embedding_model.py --repo BAAI/bge-m3 --dest E:\\agent\\models\\bge-m3
  python scripts/download_embedding_model.py --repo BAAI/bge-reranker-v2-m3 --dest E:\\agent\\models\\bge-reranker-v2-m3
"""
from __future__ import annotations

import argparse
import os
import pathlib
import sys

DEFAULT_MIRROR = "https://hf-mirror.com"

# 只取推理需要的权重：跳过 onnx/openvino/图片等冗余大文件
IGNORE = ["onnx/*", "openvino/*", "imgs/*", "*.onnx", "*.h5", "*.msgpack", "*.ot", "*.jpg", "*.png"]


def main() -> int:
    ap = argparse.ArgumentParser(description="下载 HF 模型权重到本地目录")
    ap.add_argument("--repo", required=True, help="HF 仓库 id，例如 BAAI/bge-m3")
    ap.add_argument("--dest", required=True, help="本地目标目录")
    ap.add_argument("--revision", default=None, help="指定 revision（生产建议锁定 commit）")
    ap.add_argument("--mirror", default=None, help=f"HF 镜像，默认 {DEFAULT_MIRROR}；传空字符串则用官方站点")
    ap.add_argument("--all-files", action="store_true", help="不做忽略，下载仓库全部文件")
    a = ap.parse_args()

    mirror = a.mirror if a.mirror is not None else DEFAULT_MIRROR
    if mirror:
        os.environ["HF_ENDPOINT"] = mirror
    print(f"[i] HF_ENDPOINT={os.environ.get('HF_ENDPOINT', '(official)')}")

    from huggingface_hub import snapshot_download

    dest = pathlib.Path(a.dest).resolve()
    dest.mkdir(parents=True, exist_ok=True)
    kwargs = dict(repo_id=a.repo, local_dir=str(dest), revision=a.revision)
    if not a.all_files:
        kwargs["ignore_patterns"] = IGNORE
    path = snapshot_download(**kwargs)
    print(f"[ok] 已下载 {a.repo} -> {path}")

    total = 0
    for p in sorted(dest.rglob("*")):
        if p.is_file():
            total += p.stat().st_size
    print(f"[ok] 目录大小约 {total / 1024 / 1024:.1f} MB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())