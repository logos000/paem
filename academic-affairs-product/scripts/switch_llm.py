# -*- coding: utf-8 -*-
"""Switch the product's answering LLM between the campus gateway and the
local Ollama endpoint.  Never prints .env contents, and never stores the
gateway key in source: the key comes from ZJU_GATEWAY_API_KEY or is carried
over from the modelgateway entry already present in .env.

Usage:
  python scripts/switch_llm.py gateway   # campus gateway deepseek-v4-flash-780k-local
  python scripts/switch_llm.py local     # 127.0.0.1:11434/v1 + qwen3.8:27b-iq4xs
"""
import os
import re
import sys
from pathlib import Path

ENV = Path(__file__).resolve().parents[1] / ".env"

GATEWAY_BASE_URL = "https://your-gateway.example.com/v1"
GATEWAY_MODEL = "deepseek-v4-flash-780k-local"
LOCAL_BLOCK = """\
LLM_BASE_URL=http://127.0.0.1:11434/v1
LLM_API_KEY=local
LLM_MODEL=qwen3.8:27b-iq4xs
LLM_EXTRA_BODY={"reasoning_effort": "none"}
"""
KEYS = ("LLM_BASE_URL", "LLM_API_KEY", "LLM_MODEL", "LLM_EXTRA_BODY", "LLM_MAX_TOKENS")


def _gateway_key(current: dict[str, str]) -> str:
    key = os.environ.get("ZJU_GATEWAY_API_KEY", "").strip()
    if key:
        return key
    if str(current.get("LLM_BASE_URL", "")).startswith("https://modelgateway"):
        key = str(current.get("LLM_API_KEY", "")).strip()
        if key:
            return key
    sys.exit("gateway key not found: set ZJU_GATEWAY_API_KEY (or keep a modelgateway entry in .env)")


def _strip_marks(lines: list[str]) -> list[str]:
    return [re.sub(r"^#(local|remote)-llm ", "", line) for line in lines]


def switch(target: str) -> None:
    lines = _strip_marks(ENV.read_text(encoding="utf-8").splitlines())
    current = {line.split("=")[0]: line.split("=", 1)[1] for line in lines if "=" in line}
    kept = [line for line in lines
            if line.split("=")[0] not in KEYS and not line.startswith(("#local-llm", "#remote-llm"))]
    if target == "gateway":
        block = [f"LLM_BASE_URL={GATEWAY_BASE_URL}",
                 f"LLM_API_KEY={_gateway_key(current)}",
                 f"LLM_MODEL={GATEWAY_MODEL}"]
    else:
        block = LOCAL_BLOCK.splitlines()
    lines = kept + [f"#active: {target}"] + block
    ENV.write_text("\n".join(lines) + "\n", encoding="utf-8")
    active = [line.split("=")[0] for line in lines if line.split("=")[0] in KEYS]
    print(f"switched to {target}; active LLM keys: {active}")


if __name__ == "__main__":
    if len(sys.argv) != 2 or sys.argv[1] not in ("gateway", "local"):
        sys.exit(__doc__)
    switch(sys.argv[1])
