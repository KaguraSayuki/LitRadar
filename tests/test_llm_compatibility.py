"""Exercise custom services through the real SDK and an offline HTTP transport."""
import copy
import json

import httpx
import openai
import pytest

from litradar.config import LLMConfig
from litradar.llm import DeepSeek, LLMClient, LLMError, parse_json


SAMPLE = {"scores": [{"id": 1, "score": 85, "reason": "研究主题相关"}],
          "summary": {"title_zh": "可见光光催化", "one_liner": "研究可见光催化反应"}}


def completion(content, **choice):
    return httpx.Response(200, json={"id": "test", "object": "chat.completion",
        "created": 1, "model": "custom-text-model", "choices": [{"index": 0,
            "finish_reason": "stop", "message": {"role": "assistant", "content": content},
            **choice}]})


def unsupported(param):
    return httpx.Response(400, json={"error": {"message": f"Unsupported parameter: {param}",
        "code": "unsupported_parameter", "param": param}})


@pytest.fixture
def service(monkeypatch):
    monkeypatch.setenv("LITRADAR_TEST_COMPAT_KEY", "dummy-test-key")
    clients = []

    def make(responses, **options):
        requests = []
        responses = iter(responses)

        def handle(request):
            requests.append(request)
            return next(responses)

        cfg = LLMConfig(base_url="https://custom.example.invalid/proxy/v1",
                        model="custom-text-model", api_key_env="LITRADAR_TEST_COMPAT_KEY", **options)
        client = LLMClient(cfg)
        sdk = openai.OpenAI(api_key=cfg.api_key, base_url=cfg.base_url, max_retries=0,
                            http_client=httpx.Client(transport=httpx.MockTransport(handle)))
        client._client = sdk
        clients.append(sdk)
        return client, requests

    yield make
    for client in clients:
        client.close()


def test_custom_base_model_and_key_are_used_by_sdk(service):
    client, requests = service([completion(json.dumps(SAMPLE))])
    assert "兼容性测试通过" in client.check_compatibility()
    request = requests[0]
    assert str(request.url) == "https://custom.example.invalid/proxy/v1/chat/completions"
    assert request.headers["Authorization"] == "Bearer dummy-test-key"
    payload = json.loads(request.content)
    assert payload["model"] == "custom-text-model"
    assert payload["response_format"] == {"type": "json_object"}
    assert "JSON" in payload["messages"][0]["content"]
    assert DeepSeek is LLMClient  # Existing integrations can retain their import.


def test_models_uses_catalog_endpoint_and_keeps_provider_model_identifiers(service):
    client, requests = service([httpx.Response(200, json={"object": "list", "data": [
        {"id": "vendor/reasoning-model"}, {"id": "Custom-Model"}, {"id": "vendor/reasoning-model"},
        {"id": ""}, {"id": None}]})])
    assert client.list_models() == ["Custom-Model", "vendor/reasoning-model"]
    assert len(requests) == 1 and requests[0].method == "GET"
    assert str(requests[0].url) == "https://custom.example.invalid/proxy/v1/models"
    assert requests[0].headers["Authorization"] == "Bearer dummy-test-key"
    assert not requests[0].content


@pytest.mark.parametrize("response,match", [
    (httpx.Response(404, json={"error": {"message": "Not found"}}), "手动填写"),
    (httpx.Response(200, json={"data": []}), "没有返回可选模型"),
    (httpx.Response(200, json={"unexpected": []}), "格式不兼容"),
    (httpx.Response(401, json={"error": {"message": "dummy-test-key invalid"}}), "密钥"),
])
def test_model_catalog_errors_are_actionable_and_redacted(service, response, match):
    client, _ = service([response])
    with pytest.raises(LLMError, match=match) as error:
        client.list_models()
    assert "dummy-test-key" not in str(error.value)


def test_explicit_parameter_rejections_adapt_once_and_persist(service):
    client, requests = service([unsupported("response_format"), unsupported("max_tokens"),
        unsupported("temperature"), completion(json.dumps(SAMPLE)), completion('{"ok": true}')])
    result = client.check_compatibility()
    assert "提示词" in result and "max_completion_tokens" in result and "默认值" in result
    assert client.json("system", "second batch") == {"ok": True}
    bodies = [json.loads(r.content) for r in requests]
    assert len(bodies) == 5
    assert "response_format" not in bodies[1]
    assert "max_tokens" not in bodies[2] and bodies[2]["max_completion_tokens"] == 1024
    assert "temperature" not in bodies[3]
    assert not {"response_format", "temperature", "max_tokens"} & bodies[4].keys()


def test_manual_compatibility_can_omit_optional_parameters(service):
    client, requests = service([completion('{"ok": true}')], json_mode="prompt",
        token_limit_parameter="max_completion_tokens", temperature=None)
    client.json("system", "user", max_tokens=300)
    payload = json.loads(requests[0].content)
    assert payload["max_completion_tokens"] == 300
    assert not {"response_format", "temperature", "max_tokens"} & payload.keys()


@pytest.mark.parametrize("options,param", [({"json_mode": "json_object"}, "response_format"),
    ({"token_limit_parameter": "max_tokens"}, "max_tokens"), ({}, "model")])
def test_explicit_modes_and_unrelated_parameters_are_not_changed(service, options, param):
    client, requests = service([unsupported(param)], **options)
    with pytest.raises(LLMError, match="参数"):
        client.json("system", "user")
    assert len(requests) == 1


@pytest.mark.parametrize("status", [400, 401, 429, 503])
def test_other_errors_do_not_trigger_compatibility_fallback_or_echo_provider_data(service, status):
    response = httpx.Response(status, json={"error": {"message": "dummy-test-key: invalid temperature",
        "param": "temperature", "code": "invalid_request_error"}})
    client, requests = service([response])
    with pytest.raises(LLMError) as error:
        client.json("system", "user")
    assert len(requests) == 1
    assert "dummy-test-key" not in str(error.value)
    assert client.compatibility_notes == []


@pytest.mark.parametrize("response", [completion(""),
    completion('{"ok": true}', finish_reason="length"),
    completion(None, message={"role": "assistant", "refusal": "cannot comply"}),
    httpx.Response(200, json={"choices": []})])
def test_empty_refused_and_truncated_results_are_rejected(service, response):
    client, _ = service([response])
    with pytest.raises(LLMError):
        client.json("system", "user")


@pytest.mark.parametrize("raw", ['[{"ok":true}]', 'null', '{"score": NaN}', '{"truncated":'])
def test_json_requires_an_object_and_rejects_nonstandard_constants(raw):
    with pytest.raises(LLMError):
        parse_json(raw)


@pytest.mark.parametrize("raw", ['```json\n{"ok": true}\n```', 'Result: {"ok": true}'])
def test_prompt_only_json_tolerates_fences_and_surrounding_text(raw):
    assert parse_json(raw) == {"ok": True}


@pytest.mark.parametrize("field,value", [("scores", ["invalid"]),
    ("summary", {"title_zh": "标题", "one_liner": {"text": "invalid"}})])
def test_reachable_service_must_pass_business_structure_check(service, field, value):
    sample = copy.deepcopy(SAMPLE)
    sample[field] = value
    client, _ = service([completion(json.dumps(sample))])
    with pytest.raises(LLMError):
        client.check_compatibility()


@pytest.mark.parametrize("field,value", [("id", True), ("id", 2),
    ("score", True), ("score", "85"), ("score", 101), ("reason", None)])
def test_invalid_score_fields_are_not_accepted(service, field, value):
    sample = copy.deepcopy(SAMPLE)
    sample["scores"][0][field] = value
    client, _ = service([completion(json.dumps(sample))])
    with pytest.raises(LLMError):
        client.check_compatibility()
