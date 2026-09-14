"""Text and JSON calls through an OpenAI Chat Completions compatible API."""
from __future__ import annotations

import json
import re
from typing import Any

from .config import LLMConfig

_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.M)


class LLMError(RuntimeError):
    pass


class LLMClient:
    def __init__(self, cfg: LLMConfig):
        self.cfg = cfg
        self._client = None
        self._use_json_format = cfg.json_mode != "prompt"
        self._token_parameter = ("max_tokens" if cfg.token_limit_parameter == "auto"
                                 else cfg.token_limit_parameter)
        self._send_temperature = cfg.temperature is not None
        self.compatibility_notes: list[str] = []

    @property
    def available(self) -> bool:
        return bool(self.cfg.enabled and self.cfg.api_key)

    def _client_or_raise(self):
        if self._client is not None:
            return self._client
        if not self.cfg.api_key:
            raise LLMError("模型密钥未设置，请在“数据与邮箱”中保存 API 密钥。")
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
        if self.cfg.json_mode not in ("auto", "json_object", "prompt"):
            raise LLMError("JSON 输出方式无效，请重新选择兼容选项。")
        if self.cfg.token_limit_parameter not in ("auto", "max_tokens", "max_completion_tokens"):
            raise LLMError("输出长度参数无效，请重新选择兼容选项。")
        client = self._client_or_raise()
        from openai import APIError

        if json_mode:
            system += "\nReturn exactly one JSON object, without Markdown fences or additional text."
        # Each adjustment is one-way and retained for later batches in this job.
        # Only explicit unsupported-parameter errors trigger compatibility retries.
        for _ in range(4):
            kwargs: dict[str, Any] = {
                "model": self.cfg.model,
                "messages": [{"role": "system", "content": system},
                             {"role": "user", "content": user}],
            }
            if self._token_parameter == "max_completion_tokens":
                # extra_body works with the project's older OpenAI SDK minimum too.
                kwargs["extra_body"] = {"max_completion_tokens": max_tokens}
            else:
                kwargs["max_tokens"] = max_tokens
            if self._send_temperature:
                kwargs["temperature"] = self.cfg.temperature
            if json_mode and self._use_json_format:
                kwargs["response_format"] = {"type": "json_object"}
            try:
                resp = client.chat.completions.create(**kwargs)
                break
            except APIError as error:
                if self._adapt(error, kwargs):
                    continue
                raise LLMError(_api_error_message(error)) from error
        else:  # Defensive bound; at most three settings can change.
            raise LLMError("接口参数仍不兼容，请调整兼容选项后重新测试。")
        choices = getattr(resp, "choices", None)
        if not isinstance(choices, list) or not choices:
            raise LLMError("模型没有返回对话结果，请检查是否使用 Chat Completions 兼容接口。")
        choice = choices[0]
        if getattr(choice, "finish_reason", None) == "length":
            raise LLMError("模型输出被截断，未采用不完整结果。请检查模型的输出长度和推理设置。")
        message = getattr(choice, "message", None)
        if getattr(message, "refusal", None) or getattr(choice, "finish_reason", None) == "content_filter":
            raise LLMError("模型拒绝了本次请求，未生成可用结果。")
        content = getattr(message, "content", None)
        if not isinstance(content, str) or not content.strip():
            raise LLMError("模型返回空内容或非文本结果，请选择支持文本输出的模型。")
        return content

    def _adapt(self, error, sent: dict) -> bool:
        if getattr(error, "status_code", None) not in (400, 422):
            return False
        body = getattr(error, "body", None)
        if isinstance(body, dict) and isinstance(body.get("error"), dict):
            body = body["error"]
        body = body if isinstance(body, dict) else {}
        message = str(body.get("message") or getattr(error, "message", "")).lower()
        code = str(body.get("code") or "").lower()
        param = str(body.get("param") or "").lower()
        unsupported = code in ("unsupported_parameter", "unknown_parameter", "unsupported_value") or any(
            text in message for text in ("not supported", "unsupported", "unrecognized", "unknown parameter",
                                         "not allowed", "unexpected keyword", "not compatible", "不支持"))
        if not unsupported:
            return False
        def mentions(name):
            return param == name or re.search(r"\b" + re.escape(name) + r"\b", message)
        if "response_format" in sent and self.cfg.json_mode == "auto" and mentions("response_format"):
            self._use_json_format = False
            self.compatibility_notes.append("JSON 使用提示词约束并校验")
        elif "max_tokens" in sent and self.cfg.token_limit_parameter == "auto" and mentions("max_tokens"):
            self._token_parameter = "max_completion_tokens"
            self.compatibility_notes.append("输出长度使用 max_completion_tokens")
        elif "temperature" in sent and mentions("temperature"):
            self._send_temperature = False
            self.compatibility_notes.append("随机程度使用模型默认值")
        else:
            return False
        return True

    def json(self, system: str, user: str, *, max_tokens: int = 2048) -> dict:
        raw = self.chat(system, user, json_mode=True, max_tokens=max_tokens)
        return parse_json(raw)

    def list_models(self) -> list[str]:
        """Read the standard model catalog without making an inference request."""
        client = self._client_or_raise()
        from openai import APIError
        try:
            response = client.models.list()
        except APIError as error:
            if getattr(error, "status_code", None) in (404, 405, 501):
                raise LLMError("此服务未提供模型列表接口，可以手动填写模型名称。") from error
            raise LLMError(_api_error_message(error)) from error
        data = getattr(response, "data", None)
        if not isinstance(data, list):
            raise LLMError("模型列表格式不兼容，请手动填写模型名称。")
        ids = {entry.id.strip() for entry in data if isinstance(getattr(entry, "id", None), str)
               and entry.id.strip() and len(entry.id) <= 2000 and not any(ord(c) < 32 for c in entry.id)}
        if not ids:
            raise LLMError("服务没有返回可选模型，请检查密钥权限，或手动填写模型名称。")
        return sorted(ids, key=str.casefold)

    def check_compatibility(self) -> str:
        data = self.json(
            "You validate a literature assistant connection. Return JSON only.",
            'For paper 1, titled "Visible-light photocatalysis", return an object with '
            '"scores": [{"id": 1, "score": a number from 0 to 100, "reason": a short string}], '
            'and "summary": {"title_zh": a Chinese translation of the title, '
            '"one_liner": a short Chinese sentence describing its topic}.',
            max_tokens=1024,
        )
        validate_scores(data, 1)
        validate_text(data.get("summary"), ("title_zh", "one_liner"))
        notes = "；".join(self.compatibility_notes)
        return "兼容性测试通过，评分与摘要的小样例结构有效。" + (notes + "。" if notes else "")


def _api_error_message(error) -> str:
    status = getattr(error, "status_code", None)
    if status in (401, 403):
        return "模型服务拒绝了密钥，请检查 API 密钥和模型访问权限。"
    if status == 404:
        return "找不到模型接口，请检查 API 根地址和模型名称。"
    if status == 429:
        return "模型服务限流或额度不足，请稍后重试并检查可用额度。"
    if status in (400, 422):
        return "模型未接受请求参数，请检查模型名称及高级兼容选项。"
    return "模型调用失败，请检查网络、服务地址或服务状态后重试。"


# Existing imports and deployments continue to work during migration.
DeepSeek = LLMClient


def validate_items(entries, count: int) -> list[dict]:
    """Reject malformed or ambiguous batch IDs before associating results with papers."""
    if not isinstance(entries, list) or not entries:
        raise LLMError("模型返回的文献列表无效，请调整 JSON 输出方式或更换模型。")
    seen = set()
    for entry in entries:
        ident = entry.get("id") if isinstance(entry, dict) else None
        if type(ident) is not int or not 1 <= ident <= count or ident in seen:
            raise LLMError("模型返回的文献编号无效或重复，未采用此次结果。")
        seen.add(ident)
    return entries


def validate_text(data, required: tuple[str, ...], optional: tuple[str, ...] = ()) -> dict:
    """Validate text fields without turning missing or structured content into summaries."""
    if not isinstance(data, dict):
        raise LLMError("模型返回的摘要结构无效，请调整 JSON 输出方式或更换模型。")
    out = {}
    for key in (*required, *optional):
        value = data.get(key)
        if value is None and key in optional:
            continue
        if not isinstance(value, str) or (key in required and not value.strip()):
            raise LLMError("模型返回的文本字段缺失或格式错误，未采用此次结果。")
        out[key] = value.strip()
    return out


def validate_scores(data: dict, count: int) -> list[dict]:
    entries = validate_items(data.get("scores"), count)
    for entry in entries:
        value = entry.get("score")
        # Bounds also reject NaN/Infinity and avoid coercing arbitrary provider data.
        if type(value) not in (int, float) or not 0 <= value <= 100:
            raise LLMError("模型返回的评分必须是 0 至 100 的数值，未采用此次结果。")
        validate_text(entry, ("reason",))
    return entries


def parse_json(raw: str) -> dict:
    """容错解析:去掉 markdown 围栏,再尝试截取最外层 {...}。"""
    if not raw:
        raise LLMError("模型返回空内容")
    s = _FENCE_RE.sub("", raw.strip()).strip()
    if s.startswith('['):
        raise LLMError("模型必须返回 JSON 对象，请调整 JSON 输出方式或更换模型。")
    def reject_constant(value):
        raise ValueError(value)
    try:
        result = json.loads(s, parse_constant=reject_constant)
    except (json.JSONDecodeError, ValueError):
        result = None
    else:
        if not isinstance(result, dict):
            raise LLMError("模型必须返回 JSON 对象，请调整 JSON 输出方式或更换模型。")
        return result
    start, end = s.find("{"), s.rfind("}")
    if start >= 0 and end > start:
        try:
            return json.loads(s[start:end + 1], parse_constant=reject_constant)
        except (json.JSONDecodeError, ValueError) as error:
            raise LLMError("模型返回的 JSON 无法解析，未采用此次结果。请调整 JSON 输出方式后重试。") from error
    raise LLMError("模型没有返回 JSON 对象，请调整 JSON 输出方式或更换模型。")
