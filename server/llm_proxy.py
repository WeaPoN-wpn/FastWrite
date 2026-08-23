"""LLM 代理：识别文本的纠错 / 润色（OpenAI 兼容协议，DeepSeek 为主）。

配置读取 server/config.json（API Key 仅存后端，前端不暴露）：
{
  "provider": "deepseek",
  "base_url": "https://api.deepseek.com",
  "api_key":  "sk-...",
  "model":    "deepseek-v4-flash",
  "timeout":  10
}

设计原则（PRD FR-3.4）：任何失败（未配置/网络/超时）都静默返回原文，
不影响主流程。
"""

from __future__ import annotations

import json
import pathlib

CONFIG_FILE = pathlib.Path(__file__).resolve().parent / "config.json"

DEFAULTS = {
    "provider": "deepseek",
    "base_url": "https://api.deepseek.com",
    "model": "deepseek-v4-flash",
    "timeout": 10,
}

SYSTEM_CORRECT = (
    "你是中文语音听写纠错助手。输入是语音识别的原始文本，可能包含同音字错别字。"
    "请只修正明显的识别错误，不改变措辞和语序，不增删内容。"
    "如果提供了专有名词表，表中词汇是强约束：凡读音相近处必须优先纠正为表中写法。"
    "标点保持原样或仅在明显错误时修正。只输出纠正后的文本，不要任何解释。"
)

SYSTEM_POLISH = (
    "你是中文口述文字整理助手。输入是语音识别的原始文本。"
    "请去除口头禅和冗余填充词（嗯、啊、那个、就是说等），理顺语序和标点，"
    "使文字通顺自然，但必须严格保持原意，不增加、不删除任何实质信息。"
    "如果提供了专有名词表，表中写法为强约束，必须原样保留。"
    "只输出整理后的文本，不要任何解释。"
)


def load_config() -> dict | None:
    try:
        cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not cfg.get("api_key"):
        return None
    merged = {**DEFAULTS, **cfg}
    return merged


def rewrite(text: str, mode: str = "polish", hotwords: list[str] | None = None) -> dict:
    """返回 {"text": ..., "rewritten": bool}。失败时 rewritten=False 且 text 为原文。"""
    original = text
    if not text or not text.strip():
        return {"text": text, "rewritten": False}
    if mode not in ("correct", "polish"):
        return {"text": text, "rewritten": False}

    cfg = load_config()
    if cfg is None:
        return {"text": original, "rewritten": False}

    system = SYSTEM_CORRECT if mode == "correct" else SYSTEM_POLISH
    user = text
    hw = " ".join(w for w in (hotwords or []) if w and w.strip())
    if hw:
        user = f"专有名词表：{hw}\n\n原始文本：{text}"

    try:
        from openai import OpenAI
        client = OpenAI(api_key=cfg["api_key"], base_url=cfg["base_url"],
                        timeout=cfg["timeout"], max_retries=0)
        resp = client.chat.completions.create(
            model=cfg["model"],
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            stream=False,
            temperature=0.3 if mode == "correct" else 0.7,
        )
        out = (resp.choices[0].message.content or "").strip()
    except Exception:
        return {"text": original, "rewritten": False}

    if not out:
        return {"text": original, "rewritten": False}
    return {"text": out, "rewritten": out != original}
