from __future__ import annotations

import hashlib
import json
import os
import sys
import time

provider_id = ""
model_id = ""


for line in sys.stdin:
    request = json.loads(line)
    request_id = request["id"]
    action = request["action"]
    payload = request.get("payload", {})
    if action == "open":
        mode = payload["mode"]
        provider_id = "rapid-compatibility" if mode == "compatibility" else f"paddle-{mode}"
        model_id = f"fake-{mode}"
        result = {
            "provider_id": provider_id,
            "model_id": model_id,
            "duration_us": 2_400_000,
            "fps": 25.0,
        }
    elif action == "recognize":
        delay_ms = int(os.environ.get("DUCAI_FAKE_OCR_DELAY_MS", "0"))
        if delay_ms > 0:
            time.sleep(delay_ms / 1_000)
        rows = []
        for timestamp_us in payload["timestamps_us"]:
            for band in payload["bands"]:
                if band["name"] != "upper":
                    continue
                frame_id = f"frame-{timestamp_us:012d}"
                text = (
                    "遗漏字幕"
                    if os.environ.get("DUCAI_FAKE_OCR_UNMATCHED_LAST") == "1"
                    and timestamp_us >= 4_800_000
                    else "我要修炼成仙"
                )
                digest = hashlib.sha256(
                    f"{provider_id}|{frame_id}|upper|{text}".encode()
                ).hexdigest()[:24]
                rows.append(
                    {
                        "id": f"ocr-{digest}",
                        "frame_id": frame_id,
                        "timestamp_us": timestamp_us,
                        "band": "upper",
                        "text": text,
                        "confidence": 0.97,
                        "provider_id": provider_id,
                        "model_id": model_id,
                        "box": [0.1, 0.2, 0.8, 0.5],
                    }
                )
        result = {
            "evidence": rows,
            "processed_until_us": payload["timestamps_us"][-1],
            "frame_count": len(payload["timestamps_us"]),
        }
    elif action in {"close", "shutdown"}:
        result = {"stopped": True}
    else:
        result = {}
    response = {
        "protocol_version": 1,
        "id": request_id,
        "ok": True,
        "result": result,
    }
    sys.stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
    sys.stdout.flush()
    if action == "shutdown":
        break
