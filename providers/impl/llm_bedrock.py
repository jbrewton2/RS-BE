from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional

import boto3
from botocore.config import Config

from providers.llm import LLMProvider


def _env(name: str, default: str = "") -> str:
    return (os.getenv(name, default) or "").strip()


class BedrockLLMProvider(LLMProvider):
    """
    Bedrock LLM + Embeddings provider.

    Env:
      - AWS_REGION (or AWS_DEFAULT_REGION)
      - BEDROCK_MODEL_ID (text gen)
      - BEDROCK_EMBED_MODEL_ID (embeddings)  (recommend: amazon.titan-embed-text-v2:0)

    Notes:
      - For Anthropic Claude on Bedrock we use the Messages API payload shape.
        Docs: anthropic_version must be "bedrock-2023-05-31".
      - For Titan embeddings v2 we use: {"inputText": "..."} and read {"embedding":[...]}.
    """

    def __init__(self) -> None:
        region = _env("AWS_REGION") or _env("AWS_DEFAULT_REGION")
        if not region:
            raise RuntimeError("AWS_REGION is required for Bedrock provider")

        self.model_id = _env("BEDROCK_MODEL_ID")
        self.embed_model_id = _env("BEDROCK_EMBED_MODEL_ID")

        if not self.model_id:
            raise RuntimeError("BEDROCK_MODEL_ID is required when LLM_PROVIDER=bedrock")

        if not self.embed_model_id:
            raise RuntimeError("BEDROCK_EMBED_MODEL_ID is required when LLM_PROVIDER=bedrock")

        cfg = Config(retries={"max_attempts": 8, "mode": "standard"}, region_name=region)
        self.client = boto3.client("bedrock-runtime", config=cfg)

    @classmethod
    def from_env(cls) -> "BedrockLLMProvider":
        return cls()

    def generate(self, prompt: str, **kwargs: Any) -> Dict[str, Any]:
        """
        Returns: {"text": "...", "provider": "bedrock", "model_id": "..."}
        """
        max_tokens = int(kwargs.get("max_tokens") or _env("LLM_MAX_TOKENS", "256") or "256")
        temperature = float(kwargs.get("temperature") or _env("LLM_TEMPERATURE", "0") or "0")
        top_p = float(kwargs.get("top_p") or _env("LLM_TOP_P", "1") or "1")

        # Anthropic Claude Messages API payload
        body = {
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": max_tokens,
            "temperature": temperature,
            "top_p": top_p,
            "messages": [
                {
                    "role": "user",
                    "content": [{"type": "text", "text": prompt}],
                }
            ],
        }

        resp = self.client.invoke_model(
            modelId=self.model_id,
            contentType="application/json",
            accept="application/json",
            body=json.dumps(body).encode("utf-8"),
        )

        raw = resp["body"].read().decode("utf-8", errors="ignore")
        data = json.loads(raw)

        # Claude Messages response typically: {"content":[{"type":"text","text":"..."}], ...}
        text_out = ""
        content = data.get("content") or []
        if isinstance(content, list) and content:
            first = content[0] or {}
            text_out = (first.get("text") or "").strip()

        return {"text": text_out, "provider": "bedrock", "model_id": self.model_id}

    def embed_texts(self, texts: List[str]) -> List[List[float]]:
        """
        Titan Text Embeddings V2: request {"inputText": "..."} -> response {"embedding":[...]}
        """
        out: List[List[float]] = []
        for t in texts:
            body = {"inputText": t}
            resp = self.client.invoke_model(
                modelId=self.embed_model_id,
                contentType="application/json",
                accept="application/json",
                body=json.dumps(body).encode("utf-8"),
            )
            raw = resp["body"].read().decode("utf-8", errors="ignore")
            data = json.loads(raw)
            emb = data.get("embedding")
            if not isinstance(emb, list):
                raise RuntimeError("Bedrock embeddings response missing 'embedding' list")
            out.append([float(x) for x in emb])
        return out