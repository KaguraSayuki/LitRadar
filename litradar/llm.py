"""DeepSeek 客户端(OpenAI 兼容接口)。

注意:DeepSeek **不提供 embedding API**,所以粗排用 BM25 而不是向量检索。
"""
from __future__ import annotations

import json
import re
from typing import Any

from .config import LLMConfig

_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.M)


class LLMError(RuntimeError):
    pass


class DeepSeek:
    def __init__(self, cfg: LLMConfig):
        self.cfg = cfg
        self._client = None

    @property
    def available(self) -> bool:
        return bool(self.cfg.enabled and self.cfg.api_key)

    def _client_or_raise(self):
        if self._client is not None:
            return self._client
        if not self.cfg.api_key:
            raise LLMError(f"环境变量 {self.cfg.api_key_env} 未设置")
        try:
            from openai import OpenAI
        except ImportError as e:  # pragma: no cover
            raise LLMError("未安装 openai 包") from e
        self._client = OpenAI(
            api_key=self.cfg.api_key,
            base_url=self.cfg.base_url,
            timeout=self.cfg.timeout,
        )
        return self._client

    def chat(self, system: str, user: str, *, json_mode: bool = True,
             max_tokens: int = 2048) -> str:
        client = self._client_or_raise()
        from openai import APIError

        kwargs: dict[str, Any] = {
            "model": self.cfg.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": self.cfg.temperature,
            "max_tokens": max_tokens,
        }
        if json_mode:
            kwargs["response_format"] = {"type": "json_object"}
        try:
            resp = client.chat.completions.create(**kwargs)
        except APIError as e:
            # SDK 已经完成自己的重试。统一异常类型,让调用方按批次降级,
            # 而不是因一次断网/限流丢掉此前已成功返回的结果。
            raise LLMError(f"{type(e).__name__}: {e}") from e
        return resp.choices[0].message.content or ""

    def json(self, system: str, user: str, *, max_tokens: int = 2048) -> dict:
        raw = self.chat(system, user, json_mode=True, max_tokens=max_tokens)
        return parse_json(raw)


def parse_json(raw: str) -> dict:
    """容错解析:去掉 markdown 围栏,再尝试截取最外层 {...}。"""
    if not raw:
        raise LLMError("模型返回空内容")
    s = _FENCE_RE.sub("", raw.strip()).strip()
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        pass
    start, end = s.find("{"), s.rfind("}")
    if start >= 0 and end > start:
        try:
            return json.loads(s[start:end + 1])
        except json.JSONDecodeError as e:
            raise LLMError(f"JSON 解析失败: {e}; 原文前 200 字: {s[:200]}") from e
    raise LLMError(f"无法从返回中提取 JSON: {s[:200]}")
