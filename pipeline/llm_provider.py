"""
Thin provider abstraction for the narrative/prompt-builder benchmark.
Routes "gemini*" models to google-genai (Vertex AI) and everything else
to OpenAI chat.completions. Deterministic settings (temperature=0.0).
"""

import asyncio
import base64
import logging
import os

logger = logging.getLogger("LLMProvider")

# Token usage of the most recent generate() call, for cost logging in
# benchmarks. Keys: text_input_tokens, audio_input_tokens, output_tokens,
# total_input_tokens. Read it immediately after the call; arms must run
# sequentially (this is a plain module global, not task-local).
LAST_USAGE: dict = {}


def _set_usage(text_in, audio_in, out, total_in):
    global LAST_USAGE
    LAST_USAGE = {
        "text_input_tokens": text_in or 0,
        "audio_input_tokens": audio_in or 0,
        "output_tokens": out or 0,
        "total_input_tokens": total_in or 0,
    }


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


async def _generate_openai(
    model: str,
    system: str | None,
    user_parts: list,
    schema: dict | None,
    max_output_tokens: int,
    timeout_s: int,
) -> str:
    import openai

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
            u = getattr(response, "usage", None)
            if u:
                details = getattr(u, "prompt_tokens_details", None)
                audio_in = (getattr(details, "audio_tokens", 0) or 0) if details else 0
                total_in = getattr(u, "prompt_tokens", 0) or 0
                _set_usage(total_in - audio_in, audio_in, getattr(u, "completion_tokens", 0), total_in)
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

    u = getattr(response, "usage", None)
    if u:
        details = getattr(u, "prompt_tokens_details", None)
        audio_in = (getattr(details, "audio_tokens", 0) or 0) if details else 0
        total_in = getattr(u, "prompt_tokens", 0) or 0
        _set_usage(total_in - audio_in, audio_in, getattr(u, "completion_tokens", 0), total_in)
    return response.choices[0].message.content
