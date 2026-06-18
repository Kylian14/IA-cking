"""Wrapper d'appel LLM via OpenRouter
Une seule fonction publique : `chat()`
"""

from __future__ import annotations

import os
from typing import Iterable, Optional

from openai import OpenAI, OpenAIError


_client: Optional[OpenAI] = None
DEFAULT_BASE_URL = "https://openrouter.ai/api/v1"


def _get_client() -> OpenAI:
    global _client
    if _client is None:
        api_key = os.getenv("OPENROUTER_API_KEY")
        if not api_key or api_key.startswith("sk-or-your"):
            raise RuntimeError(
                "OPENROUTER_API_KEY manquant ou non configuré. "
            )
        base_url = os.getenv("OPENROUTER_BASE_URL", DEFAULT_BASE_URL)
        _client = OpenAI(api_key=api_key, base_url=base_url)
    return _client


def chat(
    model: str,
    system_prompt: str,
    history: Iterable[dict],
    user_message: str,
    temperature: float = 0.7,
    max_tokens: int = 1024,
) -> str:
    """Appelle OpenRouter et renvoie le texte de la réponse.
    `history` : liste de {role, content} (user/assistant uniquement).
    `user_message` : le nouveau message à ajouter en fin.
    """
    messages = [{"role": "system", "content": system_prompt}]
    messages.extend(history)
    messages.append({"role": "user", "content": user_message})
    extra_headers = {
        "HTTP-Referer": os.getenv("OPENROUTER_APP_URL", "https://github.com/Kylian14/IA-cking"),
        "X-Title": os.getenv("OPENROUTER_APP_TITLE", "IA'cking"),
    }

    try:
        completion = _get_client().chat.completions.create(
            model=model,
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
            extra_headers=extra_headers,
        )
    except OpenAIError as e:
        raise RuntimeError(f"Erreur OpenRouter ({model}) : {e}") from e

    if not completion.choices:
        raise RuntimeError("Réponse OpenRouter vide.")

    content = completion.choices[0].message.content
    if content is None:
        raise RuntimeError("Réponse OpenRouter sans contenu.")
    return content.strip()
