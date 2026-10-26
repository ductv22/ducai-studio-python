"""Local-only experimental demo, separate from the desktop product."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from dialogue_casting.asr_experiment import convert_model, download_model, transcribe_demo


def main() -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="backslashreplace")
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    install = commands.add_parser("download-asr")
    install.add_argument("--output", type=Path, required=True)
    convert = commands.add_parser("convert-asr")
    convert.add_argument("--source", type=Path, required=True)
    convert.add_argument("--output", type=Path, required=True)
    convert.add_argument("--ct2-site", type=Path, required=True)
    prepare = commands.add_parser("prepare")
    prepare.add_argument("--corpus", type=Path, required=True)
    prepare.add_argument("--predictions", type=Path, required=True)
    prepare.add_argument("--labels", type=Path, required=True)
    prepare.add_argument("--output", type=Path, required=True)
    prepare.add_argument("--ffmpeg", default="ffmpeg")
    asr = commands.add_parser("transcribe")
    asr.add_argument("--root", type=Path, required=True)
    asr.add_argument("--model-path", type=Path, required=True)
    asr.add_argument("--model-id", choices=["large-v3", "belle-zh"], required=True)
    asr.add_argument("--device", choices=["cpu", "cuda"], default="cuda")
    build = commands.add_parser("build")
    build.add_argument("--root", type=Path, required=True)
    srt = commands.add_parser("prepare-srt")
    srt.add_argument("--previous-root", type=Path, required=True)
    srt.add_argument("--manifest", type=Path, required=True)
    srt.add_argument("--corpus", type=Path, required=True)
    srt.add_argument("--review", type=Path, required=True)
    srt.add_argument("--output", type=Path, required=True)
    casting = commands.add_parser("cast-srt")
    casting.add_argument("--root", type=Path, required=True)
    casting.add_argument("--model-root", type=Path, required=True)
    srt_build = commands.add_parser("build-srt")
    srt_build.add_argument("--root", type=Path, required=True)
    ecapa = commands.add_parser("download-ecapa")
    ecapa.add_argument("--output", type=Path, required=True)
    refine = commands.add_parser("prepare-refined")
    refine.add_argument("--previous-root", type=Path, required=True)
    refine.add_argument("--review", type=Path, required=True)
    refine.add_argument("--output", type=Path, required=True)
    refined_casting = commands.add_parser("cast-refined")
    refined_casting.add_argument("--root", type=Path, required=True)
    refined_casting.add_argument("--model-root", type=Path, required=True)
    refined_build = commands.add_parser("build-refined")
    refined_build.add_argument("--root", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "download-ecapa":
        from dialogue_casting.ecapa import download_model as download_ecapa

        result = download_ecapa(args.output)
    elif args.command in ("prepare-refined", "cast-refined", "build-refined"):
        from dialogue_casting.refined_demo import (
            build_refined_demo,
            cast_refined_demo,
            prepare_refined_demo,
        )

        if args.command == "prepare-refined":
            result = prepare_refined_demo(args.previous_root, args.review, args.output)
        elif args.command == "cast-refined":
            result = cast_refined_demo(args.root, args.model_root)
        else:
            result = build_refined_demo(args.root)
    elif args.command == "download-asr":
        result = download_model(args.output)
    elif args.command == "convert-asr":
        result = convert_model(args.source, args.output, args.ct2_site)
    elif args.command == "transcribe":
        result = transcribe_demo(args.root, args.model_path, args.model_id, args.device)
    elif args.command in ("prepare-srt", "cast-srt", "build-srt"):
        from dialogue_casting.srt_demo import build_srt_demo, cast_srt_demo, prepare_srt_demo

        if args.command == "prepare-srt":
            result = prepare_srt_demo(
                args.previous_root, args.manifest, args.corpus, args.review, args.output
            )
        elif args.command == "cast-srt":
            result = cast_srt_demo(args.root, args.model_root)
        else:
            result = build_srt_demo(args.root)
    else:
        from dialogue_casting.demo import build_demo, prepare_demo

        if args.command == "prepare":
            result = prepare_demo(
                args.corpus, args.predictions, args.labels, args.output, args.ffmpeg
            )
        else:
            result = build_demo(args.root)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
