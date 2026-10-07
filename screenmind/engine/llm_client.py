"""
Unified LLM Client for ScreenMind
Communicates with llama-server (llama.cpp) via OpenAI-compatible API.
Supports text, vision (images), and audio input.
"""

import base64
import logging
import time
from typing import Optional, List

import httpx

from screenmind.config import settings

logger = logging.getLogger("screenmind.engine.llm_client")


# Timeout for inference calls (screenshots can take 30-60s on slow hardware)
INFERENCE_TIMEOUT = 300.0
HEALTH_TIMEOUT = 5.0


def _base_url() -> str:
    return settings.llama_server_host.rstrip("/")


def chat(
    messages: list,
    temperature: float = 0.1,
    max_tokens: int = 1024,
    timeout: float = INFERENCE_TIMEOUT,
) -> str:
    """
    Send a chat completion request to llama-server.

    Messages follow OpenAI format:
    [{"role": "user", "content": "text"}]
    or multimodal:
    [{"role": "user", "content": [
        {"type": "text", "text": "..."},
        {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,..."}},
    ]}]

    Returns the assistant's response text.
    """
    url = f"{_base_url()}/v1/chat/completions"
    payload = {
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }

    client = httpx.Client(timeout=timeout)
    try:
        response = client.post(url, json=payload)
        response.raise_for_status()
        data = response.json()
        return data["choices"][0]["message"]["content"]
    finally:
        try:
            client.close()
        except Exception:
            pass


def chat_with_images(
    prompt: str,
    images: List[bytes],
    system: Optional[str] = None,
    temperature: float = 0.1,
    max_tokens: int = 1024,
    timeout: float = INFERENCE_TIMEOUT,
) -> str:
    """
    Chat with image inputs. Convenience wrapper for vision calls.

    Args:
        prompt: User text prompt
        images: List of JPEG image bytes
        system: Optional system message
        temperature: Sampling temperature
        max_tokens: Max response tokens
    """
    content = [{"type": "text", "text": prompt}]
    for img_bytes in images:
        b64 = base64.b64encode(img_bytes).decode()
        content.append({
            "type": "image_url",
            "image_url": {"url": f"data:image/jpeg;base64,{b64}"},
        })

    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": content})

    return chat(messages, temperature=temperature, max_tokens=max_tokens, timeout=timeout)


def transcribe_audio(
    audio_bytes: bytes,
    prompt: str = "Transcribe this audio accurately. Output only the transcription, nothing else.",
    audio_format: str = "wav",
    temperature: float = 0.1,
    max_tokens: int = 1024,
    timeout: float = INFERENCE_TIMEOUT,
) -> str:
    """
    Transcribe audio using Gemma 4's native audio encoder.

    Args:
        audio_bytes: Raw audio file bytes (WAV format recommended)
        prompt: Instruction for the model
        audio_format: Audio format (wav, mp3, etc.)
        temperature: Sampling temperature
        max_tokens: Max response tokens

    Raises:
        ValueError: If the active model doesn't support audio input.
    """
    # Guard: check if active model supports audio
    from screenmind.engine import model_manager
    if not model_manager.is_audio_capable():
        active = model_manager.get_active_model() or "unknown"
        raise ValueError(
            f"Model '{active}' does not support audio input. "
            f"Switch to Gemma 4 E2B or E4B for voice memo and meeting transcription."
        )

    b64_audio = base64.b64encode(audio_bytes).decode()

    messages = [{
        "role": "user",
        "content": [
            {"type": "text", "text": prompt},
            {"type": "input_audio", "input_audio": {"data": b64_audio, "format": audio_format}},
        ],
    }]

    result = chat(messages, temperature=temperature, max_tokens=max_tokens, timeout=timeout)

    # Strip Gemma's <unusedN> garbage tokens — these appear when the audio
    # encoder can't parse the input (too short, silence, noise)
    import re
    result = re.sub(r'<unused\d+>', '', result).strip()

    return result


def generate(
    prompt: str,
    temperature: float = 0.3,
    max_tokens: int = 1024,
    timeout: float = INFERENCE_TIMEOUT,
) -> str:
    """
    Simple text generation (no conversation history).
    Replaces ollama client.generate().
    """
    messages = [{"role": "user", "content": prompt}]
    return chat(messages, temperature=temperature, max_tokens=max_tokens, timeout=timeout)


def is_available() -> bool:
    """Check if llama-server is reachable and healthy."""
    try:
        url = f"{_base_url()}/health"
        response = httpx.get(url, timeout=HEALTH_TIMEOUT)
        return response.status_code == 200
    except Exception:
        return False


def get_server_status() -> dict:
    """Get detailed server status."""
    try:
        url = f"{_base_url()}/health"
        response = httpx.get(url, timeout=HEALTH_TIMEOUT)
        if response.status_code == 200:
            return {"status": "ok", "detail": response.json() if response.text else {}}
        return {"status": "error", "detail": f"HTTP {response.status_code}"}
    except httpx.ConnectError:
        return {"status": "unreachable", "detail": "Cannot connect to llama-server"}
    except Exception as e:
        return {"status": "error", "detail": str(e)}
