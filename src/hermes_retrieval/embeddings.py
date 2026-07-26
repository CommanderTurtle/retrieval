from __future__ import annotations

from dataclasses import dataclass
import hashlib
import logging
import math
from pathlib import Path
from typing import Iterable

import httpx

from .config import Settings

logger = logging.getLogger(__name__)


def _normalize(vector: Iterable[float]) -> list[float]:
    values = [float(item) for item in vector]
    norm = math.sqrt(sum(item * item for item in values)) or 1.0
    return [item / norm for item in values]


@dataclass
class Embedder:
    lane: str
    model: str
    url: str
    dimension: int

    @property
    def fingerprint(self) -> str:
        raw = f"{self.lane}\n{self.url}\n{self.model}\n{self.dimension}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]

    def encode(self, texts: list[str]) -> list[list[float]]:
        raise NotImplementedError


class HttpEmbedder(Embedder):
    def __init__(self, settings: Settings):
        self.settings = settings
        self.client = httpx.Client(
            timeout=httpx.Timeout(connect=3.0, read=30.0, write=30.0, pool=5.0)
        )
        probe = self._encode_batches(["retrieval probe"])
        if not probe or not probe[0]:
            raise RuntimeError("embedding endpoint returned no vector")
        super().__init__(
            lane="custom",
            model=settings.embedding_model,
            url=settings.embedding_url,
            dimension=len(probe[0]),
        )

    def _encode_batches(self, texts: list[str]) -> list[list[float]]:
        out: list[list[float]] = []
        headers = {}
        if self.settings.embedding_api_key:
            headers["Authorization"] = f"Bearer {self.settings.embedding_api_key}"
        for start in range(0, len(texts), self.settings.embedding_batch_size):
            batch = [text[: self.settings.embedding_max_chars] for text in texts[start:start + self.settings.embedding_batch_size]]
            response = self.client.post(
                self.settings.embedding_url,
                headers=headers,
                json={"input": batch, "model": self.settings.embedding_model},
            )
            response.raise_for_status()
            rows = response.json().get("data", [])
            rows.sort(key=lambda row: row.get("index", 0))
            out.extend(_normalize(row["embedding"]) for row in rows)
        return out

    def encode(self, texts: list[str]) -> list[list[float]]:
        return self._encode_batches(texts)


class FastEmbedder(Embedder):
    def __init__(self, settings: Settings):
        from fastembed import TextEmbedding

        cache_dir: Path = settings.fastembed_cache
        cache_dir.mkdir(parents=True, exist_ok=True)
        self.engine = TextEmbedding(
            model_name=settings.fastembed_model,
            cache_dir=str(cache_dir),
        )
        probe = list(self.engine.embed(["retrieval probe"]))
        if not probe:
            raise RuntimeError("FastEmbed returned no vector")
        super().__init__(
            lane="fastembed",
            model=settings.fastembed_model,
            url="local://fastembed",
            dimension=len(probe[0]),
        )

    def encode(self, texts: list[str]) -> list[list[float]]:
        return [_normalize(vector) for vector in self.engine.embed(texts)]


def build_embedders(settings: Settings) -> tuple[list[Embedder], list[str]]:
    embedders: list[Embedder] = []
    errors: list[str] = []
    if settings.embedding_url:
        try:
            embedders.append(HttpEmbedder(settings))
        except Exception as exc:
            errors.append(f"custom: {type(exc).__name__}: {exc}")
            logger.warning("Custom embedding endpoint unavailable: %s", exc)
    try:
        embedders.append(FastEmbedder(settings))
    except Exception as exc:
        errors.append(f"fastembed: {type(exc).__name__}: {exc}")
        logger.warning("FastEmbed unavailable: %s", exc)
    return embedders, errors

