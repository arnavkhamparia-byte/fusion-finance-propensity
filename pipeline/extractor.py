"""
Stage 1: LLM signal extraction wrapper.
Supports Gemini (Vertex AI / Google AI API) and OpenAI audio models.
"""

import base64
import json
import os
import re
import time

import json5


def _load_prompt(prompt_file: str, current_datetime: str) -> str:
    with open(prompt_file, encoding="utf-8") as f:
        raw = f.read().strip()
    if raw.startswith('"""'): raw = raw[3:]
    if raw.endswith('"""'):   raw = raw[:-3]
    return raw.strip().replace("{current_datetime}", current_datetime)


def _extract_json(text: str) -> dict:
    text = text.replace("```json", "").replace("```", "").strip()
    for old, new in [("\\bNone\\b", "null"), ("\\bTrue\\b", "true"), ("\\bFalse\\b", "false")]:
        text = re.sub(old, new, text)
    start = text.find("{")
    if start == -1:
        raise ValueError("No JSON object found in response")
    depth = 0
    for i, ch in enumerate(text[start:], start):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                block = text[start:i + 1]
                try:
                    return json.loads(block)
                except json.JSONDecodeError:
                    return json5.loads(block)
    raise ValueError("Could not parse JSON from response")


def extract_signals_gemini(
    audio_bytes: bytes,
    mime: str,
    prompt_file: str,
    current_datetime: str,
    model: str = "gemini-2.5-flash",
    use_vertex: bool = True,
) -> dict:
    from google import genai
    from google.genai import types

    prompt = _load_prompt(prompt_file, current_datetime)

    if use_vertex:
        client = genai.Client(
            vertexai=True,
            project=os.environ.get("GCP_PROJECT_ID", "vertex-gemini-oto-cms"),
            location=os.environ.get("GCP_LOCATION", "us-central1"),
        )
    else:
        client = genai.Client(api_key=os.environ["GOOGLE_API_KEY"])

    audio_part = types.Part.from_bytes(data=audio_bytes, mime_type=mime)

    for attempt in range(3):
        try:
            response = client.models.generate_content(
                model=model,
                contents=[prompt, audio_part],
                config=types.GenerateContentConfig(response_mime_type="application/json", temperature=0.0),
            )
            break
        except Exception as e:
            err = str(e)
            if ("429" in err or "503" in err) and attempt < 2:
                wait = 60 if "429" in err else 15
                time.sleep(wait)
            else:
                raise

    parsed = _extract_json(response.text)

    # Attach real token usage for cost accounting (runner pops this key).
    u = response.usage_metadata
    if u:
        audio_tokens = sum(
            d.token_count for d in (u.prompt_tokens_details or [])
            if str(d.modality) == "MediaModality.AUDIO"
        )
        parsed["_token_usage"] = {
            "prompt_tokens": u.prompt_token_count or 0,
            "audio_tokens": audio_tokens,
            "text_input_tokens": (u.prompt_token_count or 0) - audio_tokens,
            "completion_tokens": u.candidates_token_count or 0,
            "thoughts_tokens": u.thoughts_token_count or 0,
        }
    return parsed


def extract_signals_openai(
    audio_bytes: bytes,
    mime: str,
    prompt_file: str,
    current_datetime: str,
    model: str = "gpt-audio",
) -> dict:
    prompt = _load_prompt(prompt_file, current_datetime)

    # "azure/<deployment>" routes to Azure AI Foundry; otherwise the direct OpenAI API.
    if model.startswith("azure/"):
        from openai import AzureOpenAI
        deployment = model.split("/", 1)[1]
        client = AzureOpenAI(
            azure_endpoint=os.environ["AZURE_OPENAI_ENDPOINT"],
            api_key=os.environ["AZURE_OPENAI_API_KEY"],
            api_version=os.environ.get("AZURE_OPENAI_API_VERSION", "2025-01-01-preview"),
        )
        api_model = deployment  # for Azure this is the deployment name
    else:
        from openai import OpenAI
        client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])
        api_model = model

    fmt    = "mp3" if mime == "audio/mpeg" else "wav"
    b64    = base64.b64encode(audio_bytes).decode()

    response = client.chat.completions.create(
        model=api_model,
        messages=[
            {"role": "system", "content": prompt},
            {"role": "user", "content": [
                {"type": "input_audio", "input_audio": {"data": b64, "format": fmt}},
                {"type": "text", "text": "Extract signals from this call recording and output the JSON."},
            ]},
        ],
        max_tokens=1024,
        temperature=0.0,
    )
    raw = response.choices[0].message.content or ""
    parsed = _extract_json(raw)
    # Attach real token usage for cost accounting (runner pops this key).
    u = response.usage
    td = getattr(u, "prompt_tokens_details", None)
    audio_tok = getattr(td, "audio_tokens", 0) if td else 0
    parsed["_token_usage"] = {
        "prompt_tokens":     getattr(u, "prompt_tokens", 0),
        "audio_tokens":      audio_tok,
        "text_input_tokens": getattr(u, "prompt_tokens", 0) - audio_tok,
        "completion_tokens": getattr(u, "completion_tokens", 0),
    }
    return parsed


def extract_signals_openai_transcript(
    audio_bytes: bytes,
    mime: str,
    prompt_file: str,
    current_datetime: str,
    model: str = "gpt-5.6-luna",
) -> dict:
    """Two-step path for text-only GPT models (gpt-5.6-*): transcribe audio, then extract signals from the transcript."""
    import io
    from openai import OpenAI

    client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])
    prompt = _load_prompt(prompt_file, current_datetime)

    buf = io.BytesIO(audio_bytes)
    buf.name = "call.mp3" if mime == "audio/mpeg" else "call.wav"
    transcribe_model = os.environ.get("OPENAI_TRANSCRIBE_MODEL", "whisper-1")
    transcript = client.audio.transcriptions.create(model=transcribe_model, file=buf).text

    response = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": prompt},
            {"role": "user", "content": f"Call transcript:\n{transcript}\n\nExtract signals from this call transcript and output the JSON."},
        ],
        max_completion_tokens=4096,
    )
    raw = response.choices[0].message.content or ""
    parsed = _extract_json(raw)
    u = response.usage
    parsed["_token_usage"] = {
        "prompt_tokens":     getattr(u, "prompt_tokens", 0),
        "completion_tokens": getattr(u, "completion_tokens", 0),
    }
    parsed["_transcript"] = transcript
    return parsed


def extract_signals(
    audio_bytes: bytes,
    mime: str,
    prompt_file: str,
    current_datetime: str,
    model: str = "gemini-2.5-flash",
    use_vertex: bool = True,
) -> dict:
    """Unified entry point — routes to Gemini, OpenAI, or Azure based on model name."""
    if model.startswith("gpt-5.6"):
        return extract_signals_openai_transcript(audio_bytes, mime, prompt_file, current_datetime, model)
    elif model.startswith("gpt-") or model.startswith("azure/"):
        return extract_signals_openai(audio_bytes, mime, prompt_file, current_datetime, model)
    else:
        return extract_signals_gemini(audio_bytes, mime, prompt_file, current_datetime, model, use_vertex)
