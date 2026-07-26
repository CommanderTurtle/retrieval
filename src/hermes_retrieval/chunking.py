from __future__ import annotations

import hashlib
import re
from typing import Iterator

import yaml


def stable_id(*parts: object) -> str:
    raw = "\n".join(str(part) for part in parts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def content_hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def chunk_text(text: str, max_chars: int = 4000, overlap: int = 300) -> Iterator[tuple[int, str]]:
    clean = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not clean:
        return
    cursor = 0
    index = 0
    while cursor < len(clean):
        end = min(len(clean), cursor + max_chars)
        if end < len(clean):
            window = clean[cursor:end]
            boundary = max(window.rfind("\n\n"), window.rfind("\n#"), window.rfind(". "))
            if boundary >= max_chars // 2:
                end = cursor + boundary + 1
        piece = clean[cursor:end].strip()
        if piece:
            yield index, piece
            index += 1
        if end >= len(clean):
            break
        cursor = max(cursor + 1, end - overlap)


def markdown_title(text: str, fallback: str) -> str:
    match = re.search(r"(?m)^#\s+(.+?)\s*$", text)
    return match.group(1).strip() if match else fallback


def frontmatter(text: str) -> dict[str, str]:
    if not text.startswith("---\n"):
        return {}
    end = text.find("\n---", 4)
    if end < 0:
        return {}
    try:
        payload = yaml.safe_load(text[4:end]) or {}
    except yaml.YAMLError:
        return {}
    if not isinstance(payload, dict):
        return {}
    result: dict[str, str] = {}
    for key, value in payload.items():
        if isinstance(value, str) and value.strip():
            result[str(key)] = value.strip()
    return result
