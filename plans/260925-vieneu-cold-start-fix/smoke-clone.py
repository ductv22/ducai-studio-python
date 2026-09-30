"""Read-only job configuration replay; audio is written only to an isolated output."""

import argparse
import hashlib
import json
import sqlite3
import time
import wave
from pathlib import Path

from ducai_studio.providers.tts.contracts import TtsCueRequest
from ducai_studio.providers.tts.vieneu import VieNeuTtsProvider


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--job", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    with sqlite3.connect(args.database.resolve().as_uri() + "?mode=ro", uri=True) as db:
        row = db.execute("select config_json from jobs where id=?", (args.job,)).fetchone()
    config = json.loads(row[0])
    provider = VieNeuTtsProvider(
        data_root=Path(config["data_root"]),
        runtime_python=Path(config["runtime_python"]),
        profile_path=Path(config["profile_path"]),
        batch_size=config["batch_size"],
    )
    try:
        started = time.monotonic()
        health = provider.health()
        print(json.dumps({"health": health.state.value,
                          "seconds": round(time.monotonic() - started, 3)}), flush=True)
        if health.state.value != "ready":
            raise RuntimeError(health.summary)
        for number in range(2):
            cues = tuple(
                TtsCueRequest(
                    cue["id"], cue["index"], cue["start_us"], cue["end_us"], cue["text"],
                    hashlib.sha256(cue["text"].encode()).hexdigest(),
                )
                for cue in config["cues"][number * 32:(number + 1) * 32]
            )
            started = time.monotonic()
            result = provider.synthesize_batch(
                request_id=f"smoke-{number}", cues=cues, voice_id=config["voice_id"],
                output_dir=args.output / str(number), batch_size=32,
            )
            if result.failures or len(result.artifacts) != len(cues):
                raise RuntimeError(str([failure.code for failure in result.failures]))
            for artifact in result.artifacts:
                with wave.open(str(artifact.wav_path), "rb") as wav:
                    assert wav.getnframes() > 0 and wav.getframerate() == 48_000
            seconds = time.monotonic() - started
            audio_seconds = sum(a.duration_us for a in result.artifacts) / 1_000_000
            print(json.dumps({"batch": number + 1, "cues": len(result.artifacts),
                              "seconds": round(seconds, 3),
                              "audio_seconds": round(audio_seconds, 3),
                              "realtime_factor": round(seconds / audio_seconds, 3)}), flush=True)
    finally:
        provider.close()


if __name__ == "__main__":
    main()
