"""
Thin provider abstraction for the narrative/prompt-builder benchmark.
Routes "gemini*" models to google-genai (Vertex AI) and everything else
to OpenAI chat.completions. Deterministic settings (temperature=0.0).
"""

import asyncio
import base64
import contextvars
import logging
import os

logger = logging.getLogger("LLMProvider")

# Token usage of the most recent generate() call, for cost logging in
# benchmarks. Keys: text_input_tokens, audio_input_tokens, output_tokens,
# total_input_tokens. Read it immediately after the call; arms must run
# sequentially (this is a plain module global, not task-local).
LAST_USAGE: dict = {}

# Task-local mirror of LAST_USAGE for concurrent harnesses: each asyncio task
# sees only the usage of calls awaited inside that task. Use reset_usage() /
# get_usage() instead of the module global when running accounts in parallel.
_usage_ctx: contextvars.ContextVar = contextvars.ContextVar("llm_usage", default=None)


def reset_usage():
    _usage_ctx.set({})


def get_usage() -> dict:
    return dict(_usage_ctx.get() or {})


def _set_usage(text_in, audio_in, out, total_in, cached_in=0):
    global LAST_USAGE
    LAST_USAGE = {
        "text_input_tokens": text_in or 0,
        "audio_input_tokens": audio_in or 0,
        "output_tokens": out or 0,
        "total_input_tokens": total_in or 0,
        "cached_input_tokens": cached_in or 0,
    }
    _usage_ctx.set(dict(LAST_USAGE))


async def generate(
    provider_model: str,
    system: str | None,
    user_parts: list,
    schema: dict | None,
    max_output_tokens: int = 8000,
    timeout_s: int = 240,
) -> str:
    """Run one generation. user_parts items are either str or
    {"audio_bytes": bytes, "mime_type": str}. Returns response text."""
    if provider_model.startswith("gemini"):
        return await _generate_gemini(
            provider_model, system, user_parts, schema, max_output_tokens, timeout_s
        )
    return await _generate_openai(
        provider_model, system, user_parts, schema, max_output_tokens, timeout_s
    )


async def _generate_gemini(
    model: str,
    system: str | None,
    user_parts: list,
    schema: dict | None,
    max_output_tokens: int,
    timeout_s: int,
) -> str:
    from google import genai
    from google.genai import types

    client = genai.Client(
        vertexai=True,
        project=os.environ.get("GCP_PROJECT_ID", "vertex-gemini-oto-cms"),
        location=os.environ.get("GCP_LOCATION", "us-central1"),
    )

    contents = []
    for part in user_parts:
        if isinstance(part, dict):
            contents.append(
                types.Part.from_bytes(data=part["audio_bytes"], mime_type=part["mime_type"])
            )
        else:
            contents.append(part)

    config = types.GenerateContentConfig(
        system_instruction=system,
        response_mime_type="application/json",
        response_schema=schema,
        max_output_tokens=max_output_tokens,
        temperature=0.0,
        top_p=0.1,
        top_k=1,
        seed=42,
    )

    response = await asyncio.wait_for(
        client.aio.models.generate_content(model=model, contents=contents, config=config),
        timeout=timeout_s,
    )
    um = getattr(response, "usage_metadata", None)
    if um:
        audio_in = 0
        for d in (getattr(um, "prompt_tokens_details", None) or []):
            if str(getattr(d, "modality", "")).endswith("AUDIO"):
                audio_in = getattr(d, "token_count", 0) or 0
        total_in = getattr(um, "prompt_token_count", 0) or 0
        _set_usage(total_in - audio_in, audio_in, getattr(um, "candidates_token_count", 0), total_in)
    return response.text


def _openai_messages(system: str | None, user_parts: list) -> list:
    content = []
    for part in user_parts:
        if isinstance(part, dict):
            content.append({
                "type": "input_audio",
                "input_audio": {
                    "data": base64.b64encode(part["audio_bytes"]).decode(),
                    "format": "mp3",
                },
            })
        else:
            content.append({"type": "text", "text": part})

    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": content})
    return messages


def _azure_client_and_deployment(model: str):
    """For "azure:<deployment>" models: build an AsyncAzureOpenAI client from
    AZURE_OPENAI_TARGET_URL (+AZURE_OPENAI_API_KEY). The target URL may be a
    full Foundry target (.../openai/deployments/<dep>/chat/completions?api-version=...)
    — endpoint, deployment and api-version are parsed out of it. A deployment
    given in the model name (azure:<dep>) or AZURE_OPENAI_DEPLOYMENT overrides
    the URL's deployment. Falls back to api-version 2024-12-01-preview."""
    import re as _re
    import openai

    target = os.environ.get("AZURE_OPENAI_TARGET_URL", "")
    api_key = os.environ.get("AZURE_OPENAI_API_KEY", "")
    if not target or not api_key:
        raise RuntimeError(
            "Azure model requested but AZURE_OPENAI_TARGET_URL / AZURE_OPENAI_API_KEY "
            "are not set in .env"
        )
    m = _re.match(r"(https://[^/]+)", target)
    if not m:
        raise RuntimeError(f"Cannot parse endpoint from AZURE_OPENAI_TARGET_URL: {target!r}")
    endpoint = m.group(1)
    url_dep = None
    dep_m = _re.search(r"/deployments/([^/?]+)", target)
    if dep_m:
        url_dep = dep_m.group(1)
    ver_m = _re.search(r"api-version=([^&]+)", target)
    api_version = ver_m.group(1) if ver_m else "2024-12-01-preview"

    name_dep = model.split(":", 1)[1] if ":" in model else ""
    deployment = name_dep or os.environ.get("AZURE_OPENAI_DEPLOYMENT") or url_dep
    if not deployment:
        raise RuntimeError(
            "No Azure deployment name: put it in the model (azure:<deployment>), "
            "in AZURE_OPENAI_DEPLOYMENT, or use a target URL containing /deployments/<name>/"
        )
    client = openai.AsyncAzureOpenAI(
        azure_endpoint=endpoint, api_key=api_key, api_version=api_version
    )
    return client, deployment


def _capture_openai_usage(response):
    u = getattr(response, "usage", None)
    if not u:
        return
    details = getattr(u, "prompt_tokens_details", None)
    audio_in = (getattr(details, "audio_tokens", 0) or 0) if details else 0
    cached_in = (getattr(details, "cached_tokens", 0) or 0) if details else 0
    total_in = getattr(u, "prompt_tokens", 0) or 0
    _set_usage(total_in - audio_in, audio_in, getattr(u, "completion_tokens", 0),
               total_in, cached_in)


async def _generate_openai(
    model: str,
    system: str | None,
    user_parts: list,
    schema: dict | None,
    max_output_tokens: int,
    timeout_s: int,
) -> str:
    import openai

    if model.startswith("azure:"):
        client, model = _azure_client_and_deployment(model)
    else:
        client = openai.AsyncOpenAI()
    kwargs = {
        "model": model,
        "messages": _openai_messages(system, user_parts),
        "temperature": 0.0,
        "max_completion_tokens": max_output_tokens,
    }
    if schema is not None:
        kwargs["response_format"] = {
            "type": "json_schema",
            "json_schema": {"name": "output", "schema": schema, "strict": False},
        }

    try:
        response = await asyncio.wait_for(
            client.chat.completions.create(**kwargs), timeout=timeout_s
        )
    except openai.BadRequestError as e:
        if "temperature" in str(e) and "temperature" in kwargs:
            # gpt-5.x reasoning models only accept the default temperature.
            logger.warning(f"{model} rejected temperature, retrying without it")
            del kwargs["temperature"]
            response = await asyncio.wait_for(
                client.chat.completions.create(**kwargs), timeout=timeout_s
            )
            _capture_openai_usage(response)
            return response.choices[0].message.content
        if "response_format" not in str(e) or "response_format" not in kwargs:
            raise
        # gpt-audio models reject response_format — retry without it.
        logger.warning(f"{model} rejected response_format, retrying without it: {e}")
        del kwargs["response_format"]
        fallback_system = ((system or "").rstrip() + "\nOUTPUT ONLY VALID JSON.").strip()
        kwargs["messages"] = _openai_messages(fallback_system, user_parts)
        response = await asyncio.wait_for(
            client.chat.completions.create(**kwargs), timeout=timeout_s
        )

    _capture_openai_usage(response)
    return response.choices[0].message.content
