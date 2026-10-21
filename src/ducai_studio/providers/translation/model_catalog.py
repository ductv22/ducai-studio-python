"""Bounded, read-only API catalog lookup; secrets never enter its result."""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from time import monotonic

from .openai_compatible import preset_for, validate_base_url


class ModelCatalogError(ValueError):
    pass


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def is_translation_model(value: str, provider_id: str) -> bool:
    """Keep discovery scoped to text models; manual entry remains unrestricted."""
    name = value.casefold()
    family = {"gemini-api": ("gemini-",), "deepseek-api": ("deepseek-",),
              "qwen-api": ("qwen", "qwq"), "glm-api": ("glm-", "chatglm")}.get(provider_id)
    if family and not name.startswith(family):
        return False
    return not re.search(
        r"(?:^|[-/._])(?:image|imagen|tts|audio|transcribe|embedding|embed|live|"
        r"computer-use|robotics|research)(?:$|[-/._])", name,
    )


def parse_model_page(payload: object, *, gemini: bool) -> tuple[list[str], str]:
    if not isinstance(payload, dict):
        raise ModelCatalogError("API trả danh sách model không đúng định dạng.")
    rows = payload.get("models" if gemini else "data")
    if not isinstance(rows, list) or len(rows) > 5000:
        raise ModelCatalogError("API trả danh sách model không đúng định dạng.")
    models = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        methods = row.get("supportedGenerationMethods", [])
        if gemini and (not isinstance(methods, list) or "generateContent" not in methods):
            continue
        value = row.get("name" if gemini else "id")
        if isinstance(value, str):
            value = value.removeprefix("models/") if gemini else value
            if gemini and not is_translation_model(value, "gemini-api"):
                continue
            if re.fullmatch(r"[\w./:@+-]{1,200}", value):
                models.append(value)
    token = payload.get("nextPageToken", "") if gemini else ""
    if not isinstance(token, str) or len(token) > 2048:
        raise ModelCatalogError("API trả phân trang model không hợp lệ.")
    return models, token


def discover_models(
    provider_id: str, secret: str, *, base_url: str = "",
    cancelled: Callable[[], bool] = lambda: False,
) -> dict:
    gemini = provider_id == "gemini-api"
    if not secret or any(char in secret for char in "\r\n\0"):
        raise ModelCatalogError("API key không hợp lệ. Hãy lưu lại key.")
    if gemini:
        endpoint = "https://generativelanguage.googleapis.com/v1beta/models"
        headers = {"x-goog-api-key": secret}
    else:
        preset = preset_for(provider_id)
        endpoint = validate_base_url(base_url if provider_id == "openai-compatible"
                                     else preset.base_url) + "/models"
        headers = {"Authorization": "Bearer " + secret}
    opener = urllib.request.build_opener(_NoRedirect())
    deadline = monotonic() + 8
    models: set[str] = set()
    token = ""
    for _ in range(2):
        if cancelled() or monotonic() >= deadline:
            raise ModelCatalogError("Đã dừng tải model. Bạn vẫn có thể nhập tên thủ công.")
        url = endpoint
        if gemini:
            query = {"pageSize": "1000"}
            if token:
                query["pageToken"] = token
            url += "?" + urllib.parse.urlencode(query)
        request = urllib.request.Request(url, headers={**headers, "Accept": "application/json"})
        try:
            with opener.open(request, timeout=min(4, deadline - monotonic())) as response:
                chunks, size = [], 0
                while True:
                    if cancelled() or monotonic() >= deadline:
                        raise ModelCatalogError("Tải model quá lâu. Hãy nhập tên thủ công.")
                    chunk = response.read1(65536)
                    if not chunk:
                        break
                    chunks.append(chunk)
                    size += len(chunk)
                    if size > 1_048_576:
                        raise ModelCatalogError("Danh sách model quá lớn. Hãy nhập tên thủ công.")
                body = b"".join(chunks)
            page, token = parse_model_page(json.loads(body), gemini=gemini)
        except urllib.error.HTTPError as exc:
            if exc.code in {401, 403}:
                message = "Key không có quyền xem model. Kiểm tra key hoặc nhập tên thủ công."
            elif exc.code == 429:
                message = "API giới hạn lượt tải model. Hãy thử lại sau."
            else:
                message = "API không hỗ trợ tải model hoặc đang lỗi. Hãy nhập tên thủ công."
            raise ModelCatalogError(message) from None
        except (OSError, ValueError) as exc:
            if isinstance(exc, ModelCatalogError):
                raise
            raise ModelCatalogError(
                "Không tải được model. Hãy thử lại hoặc nhập tên thủ công."
            ) from None
        models.update(value for value in page
                      if secret not in value and is_translation_model(value, provider_id))
        if not token:
            break
    return {"models": sorted(models), "partial": bool(token)}
