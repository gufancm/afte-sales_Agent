import os
import pathlib
import sys

import pytest


@pytest.fixture(autouse=True)
def _isolate_llm_env(monkeypatch):
    """单元测试不得受开发机 .env / compose env_file 注入的 LLM 配置影响。

    这些前缀下的环境变量会改变 deps 装配（例如 LLM_MODEL_SMALL 会让测试意外调用真实端点）。
    """
    for key in list(os.environ):
        if key.startswith(("LLM_", "EMBEDDING_", "RAG_", "BGE_M3_")):
            monkeypatch.delenv(key, raising=False)

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))