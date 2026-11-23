"""Make the gateway report the real deployment model (e.g. gemini-3.1-flash-lite,
openai/gpt-oss-20b) in the response `model` field instead of the alias "preauth-model",
so clients/tracers (ADK -> Langfuse) show which provider actually served the call."""
from litellm.integrations.custom_logger import CustomLogger


class RawModelName(CustomLogger):
    async def async_pre_call_hook(self, user_api_key_dict, cache, data, call_type):
        md = data.get("metadata")
        if not isinstance(md, dict):
            md = data["metadata"] = {}
        md["_complexity_router_return_raw_model_name"] = True
        return data


raw_model_name = RawModelName()


class CacheHitMarker(CustomLogger):
    """Flag semantic-cache hits in the response `model` field (e.g. "gemini-3.1-flash-lite (cache hit)")
    -- the only response field ADK carries through (LlmResponse.model_version) -- so the ADK agent can
    stamp it on the Langfuse span."""

    async def async_post_call_success_hook(self, data, user_api_key_dict, response):
        try:
            hp = getattr(response, "_hidden_params", None) or {}
            if hp.get("cache_hit") and getattr(response, "model", None):
                if "(cache hit)" not in response.model:
                    response.model = f"{response.model} (cache hit)"
        except Exception:
            pass
        return response


cache_hit_marker = CacheHitMarker()
