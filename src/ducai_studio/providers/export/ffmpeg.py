from __future__ import annotations

import math
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from ducai_studio.domain.effects import (
    ExportSettings,
    ExportSnapshot,
    RenderClip,
    RenderConformSegment,
    RenderOverlay,
    RenderSceneText,
    RenderSubtitle,
    RenderSubtitleBlur,
    SubtitleStyle,
    VideoFilterSettings,
)

EXPORT_AUDIO_SAMPLE_RATE = 48_000
MIX_PEAK_CEILING_DBFS = -1.5
MIX_PEAK_LIMIT = 10 ** (MIX_PEAK_CEILING_DBFS / 20.0)


def _hidden_subprocess_kwargs() -> dict[str, object]:
    if os.name != "nt":
        return {}
    startup = subprocess.STARTUPINFO()
    startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    return {"startupinfo": startup, "creationflags": subprocess.CREATE_NO_WINDOW}


@dataclass(frozen=True, slots=True)
class FfmpegCapabilities:
    ffmpeg_path: str
    version_line: str
    h264_nvenc: bool
    libx264: bool
    ass_filter: bool

    @property
    def ready(self) -> bool:
        return self.libx264 and self.ass_filter


@dataclass(frozen=True, slots=True)
class ExportChunkPlan:
    index: int
    start_us: int
    end_us: int
    output_path: Path
    ass_path: Path | None
    filter_script_path: Path
    arguments: tuple[str, ...]
    backend: str

    @property
    def duration_us(self) -> int:
        return self.end_us - self.start_us


def probe_ffmpeg_capabilities(ffmpeg_path: str | Path) -> FfmpegCapabilities:
    executable = str(Path(ffmpeg_path).expanduser().resolve(strict=True))
    version = subprocess.run(
        [executable, "-hide_banner", "-version"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=10,
        check=False,
        **_hidden_subprocess_kwargs(),
    )
    encoders = subprocess.run(
        [executable, "-hide_banner", "-encoders"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=15,
        check=False,
        **_hidden_subprocess_kwargs(),
    )
    filters = subprocess.run(
        [executable, "-hide_banner", "-filters"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=15,
        check=False,
        **_hidden_subprocess_kwargs(),
    )
    encoder_text = encoders.stdout + encoders.stderr
    filter_text = filters.stdout + filters.stderr
    nvenc_listed = "h264_nvenc" in encoder_text
    nvenc_usable = nvenc_listed and _probe_nvenc(executable)
    return FfmpegCapabilities(
        executable,
        next(iter(version.stdout.splitlines()), "ffmpeg unknown"),
        nvenc_usable,
        "libx264" in encoder_text,
        any(" ass " in f" {line} " for line in filter_text.splitlines()),
    )


def chunk_ranges(snapshot: ExportSnapshot) -> tuple[tuple[int, int], ...]:
    result: list[tuple[int, int]] = []
    cursor = snapshot.timeline_start_us
    while cursor < snapshot.timeline_end_us:
        end = min(snapshot.timeline_end_us, cursor + snapshot.settings.chunk_duration_us)
        result.append((cursor, end))
        cursor = end
    return tuple(result)


def materialize_export_chunk(
    snapshot: ExportSnapshot,
    *,
    index: int,
    start_us: int,
    end_us: int,
    work_dir: Path,
    output_path: Path,
    capabilities: FfmpegCapabilities,
) -> ExportChunkPlan:
    if not snapshot.timeline_start_us <= start_us < end_us <= snapshot.timeline_end_us:
        raise ValueError("Export chunk lies outside the immutable snapshot")
    if not capabilities.ready:
        raise RuntimeError("FFmpeg lacks libx264 or the ASS subtitle filter")
    work_dir = work_dir.expanduser().resolve()
    work_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_path.expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    duration_us = end_us - start_us
    duration_s = _seconds(duration_us)
    video = snapshot.video
    video_speed = video.speed_numerator / video.speed_denominator
    conform_pieces = _chunk_conform_pieces(snapshot, start_us, end_us)
    if conform_pieces:
        source_start_us = conform_pieces[0][0]
        source_duration_us = sum(item[1] - item[0] for item in conform_pieces)
        args = [
            capabilities.ffmpeg_path,
            "-hide_banner",
            "-nostdin",
            "-y",
            "-i",
            video.path,
        ]
    else:
        source_start_us = video.source_start_us + int(
            round((start_us - video.timeline_start_us) * video_speed)
        )
        source_duration_us = int(round(duration_us * video_speed))
        args = [
            capabilities.ffmpeg_path,
            "-hide_banner",
            "-nostdin",
            "-y",
            "-ss",
            _seconds(source_start_us),
            "-t",
            _seconds(source_duration_us),
            "-i",
            video.path,
        ]

    active_overlays = [
        overlay
        for overlay in snapshot.overlays
        if overlay.end_us > start_us and overlay.start_us < end_us
    ]
    logos = [overlay for overlay in active_overlays if overlay.kind == "logo"]
    blurs = [overlay for overlay in active_overlays if overlay.kind == "blur"]
    active_subtitle_blurs = [
        blur
        for blur in snapshot.subtitle_blurs
        if blur.end_us > start_us and blur.start_us < end_us
    ]
    active_scene_texts = [
        cue
        for cue in snapshot.scene_texts
        if cue.review_state == "accepted"
        and cue.end_us > start_us
        and cue.start_us < end_us
    ]
    logo_inputs: list[tuple[int, RenderOverlay]] = []
    next_input = 1
    for logo in logos:
        args += ["-i", logo.asset_path]
        logo_inputs.append((next_input, logo))
        next_input += 1

    active_audio: list[
        tuple[RenderClip, int, int, int, int, bool, bool, bool]
    ] = []
    for clip in snapshot.audio:
        if clip.muted or clip.volume <= 0:
            continue
        intersection_start = max(start_us, clip.timeline_start_us)
        intersection_end = min(end_us, clip.timeline_end_us)
        if intersection_end <= intersection_start:
            continue
        speed = clip.speed_numerator / clip.speed_denominator
        source_start = clip.source_start_us + int(
            round((intersection_start - clip.timeline_start_us) * speed)
        )
        source_duration = int(round((intersection_end - intersection_start) * speed))
        conformed_original = bool(
            conform_pieces and clip.track_kind == "original_audio"
        )
        same_as_video = os.path.normcase(os.path.abspath(video.path)) == os.path.normcase(
            os.path.abspath(clip.path)
        )
        reuse_video = not conformed_original and _can_reuse_video_input_for_audio(
            video=video,
            audio=clip,
            chunk_start_us=start_us,
            chunk_end_us=end_us,
            video_source_start_us=source_start_us,
            video_source_duration_us=source_duration_us,
            audio_source_start_us=source_start,
            audio_source_duration_us=source_duration,
        )
        active_audio.append(
            (
                clip,
                intersection_start,
                intersection_end,
                source_start,
                source_duration,
                conformed_original,
                same_as_video,
                reuse_video,
            )
        )

    # Cue-scoped D1 clips normally point at different source ranges inside one
    # mastered WAV. Opening that WAV once per cue makes a 10-minute Windows
    # command line exceed CreateProcess' 32K limit. Group clips by input path,
    # seek once to the smallest source interval needed by this chunk, then use
    # atrim from the filter-complex script for each cue.
    grouped_inputs: dict[tuple[str, bool], dict[str, object]] = {}
    for (
        clip,
        _intersection_start,
        _intersection_end,
        source_start,
        source_duration,
        conformed_original,
        same_as_video,
        reuse_video,
    ) in active_audio:
        if same_as_video and (conformed_original or reuse_video):
            continue
        normalized_path = os.path.normcase(os.path.abspath(clip.path))
        key = (normalized_path, conformed_original)
        group = grouped_inputs.get(key)
        if group is None:
            group = {
                "path": clip.path,
                "start_us": 0 if conformed_original else source_start,
                "end_us": 0 if conformed_original else source_start + source_duration,
            }
            grouped_inputs[key] = group
        elif not conformed_original:
            group["start_us"] = min(int(group["start_us"]), source_start)
            group["end_us"] = max(
                int(group["end_us"]), source_start + source_duration
            )

    for group in grouped_inputs.values():
        group["input_index"] = next_input
        group_start = int(group["start_us"])
        group_end = int(group["end_us"])
        if group_end > group_start:
            args += [
                "-ss",
                _seconds(group_start),
                "-t",
                _seconds(group_end - group_start),
            ]
        args += ["-i", str(group["path"])]
        next_input += 1

    audio_inputs: list[
        tuple[int, RenderClip, int, int, int, int, int, bool]
    ] = []
    for (
        clip,
        intersection_start,
        intersection_end,
        source_start,
        source_duration,
        conformed_original,
        same_as_video,
        reuse_video,
    ) in active_audio:
        if same_as_video and (conformed_original or reuse_video):
            input_index = 0
            input_anchor_us = source_start if reuse_video else 0
            input_pretrimmed = reuse_video
        else:
            key = (
                os.path.normcase(os.path.abspath(clip.path)),
                conformed_original,
            )
            group = grouped_inputs[key]
            input_index = int(group["input_index"])
            input_anchor_us = int(group["start_us"])
            input_pretrimmed = False
        audio_inputs.append(
            (
                input_index,
                clip,
                intersection_start,
                intersection_end,
                source_start,
                source_duration,
                input_anchor_us,
                input_pretrimmed,
            )
        )

    silent_input: int | None = None
    if not audio_inputs:
        silent_input = next_input
        args += [
            "-f",
            "lavfi",
            "-t",
            duration_s,
            "-i",
            "anullsrc=channel_layout=stereo:sample_rate=48000",
        ]

    output_width = snapshot.settings.output_width or snapshot.source_width
    output_height = snapshot.settings.output_height or snapshot.source_height
    output_width -= output_width % 2
    output_height -= output_height % 2
    graph: list[str] = []
    video_input_label = "0:v"
    if conform_pieces:
        piece_labels: list[str] = []
        for number, (piece_start, piece_end, piece_speed) in enumerate(conform_pieces):
            label = f"vconform{number}"
            graph.append(
                f"[0:v]trim=start={_seconds(video.source_start_us + piece_start)}:"
                f"end={_seconds(video.source_start_us + piece_end)},"
                f"setpts=(PTS-STARTPTS)/{piece_speed:.12f}[{label}]"
            )
            piece_labels.append(f"[{label}]")
        graph.append(
            "".join(piece_labels)
            + f"concat=n={len(piece_labels)}:v=1:a=0[vconformed]"
        )
        video_input_label = "vconformed"
        video_filters: list[str] = []
    else:
        video_filters = [f"setpts={1.0 / video_speed:.12f}*PTS"]
    if (output_width, output_height) != (snapshot.source_width, snapshot.source_height):
        video_filters.append(f"scale={output_width}:{output_height}:flags=lanczos")
    video_filters.extend(
        [
            f"fps={snapshot.settings.fps}",
            *_video_filter_chain(snapshot.settings.video_filter),
            "format=yuv420p",
            f"trim=duration={duration_s}",
            "setpts=PTS-STARTPTS",
        ]
    )
    graph.append(
        f"[{video_input_label}]" + ",".join(video_filters or ["null"]) + "[vprep]"
    )
    current_label = "vprep"
    blur_groups: dict[tuple[int, bool], list[RenderSubtitleBlur]] = {}
    for blur in active_subtitle_blurs:
        key = (blur.blur_radius, blur.concealment_mode == "cover")
        blur_groups.setdefault(key, []).append(blur)
    for group_number, ((requested_radius, strong_blur), group) in enumerate(
        sorted(blur_groups.items())
    ):
        base_label = f"vsubblur{group_number}base"
        source_label = f"vsubblur{group_number}src"
        graph.append(
            f"[{current_label}]split=2[{base_label}][{source_label}]"
        )
        radius = max(1, min(requested_radius, output_width // 2, output_height // 2))
        chroma_radius = max(1, min(6, radius // 2))
        blurred_label = f"vsubblur{group_number}full"
        power = 2 if strong_blur else 1
        graph.append(
            f"[{source_label}]boxblur=luma_radius={radius}:luma_power={power}:"
            f"chroma_radius={chroma_radius}:chroma_power={power}[{blurred_label}]"
        )
        mask_filters = [
            f"color=c=black:s={output_width}x{output_height}:r={snapshot.settings.fps}:"
            f"d={duration_s}"
        ]
        for blur in group:
            x, y, width, height = _overlay_pixels(blur, output_width, output_height)
            enable = _range_enable(blur.start_us, blur.end_us, start_us, end_us)
            mask_filters.append(
                f"drawbox=x={x}:y={y}:w={width}:h={height}:"
                f"color=white@{blur.opacity:.6f}:t=fill:enable='{enable}'"
            )
        mask_label = f"vsubblur{group_number}mask"
        graph.append(",".join(mask_filters) + f",format=gray[{mask_label}]")
        next_label = f"vsubblur{group_number}out"
        graph.append(
            f"[{base_label}][{blurred_label}][{mask_label}]maskedmerge[{next_label}]"
        )
        current_label = next_label
        covered = [blur for blur in group if blur.concealment_mode == "cover"]
        if covered:
            cover_filters = []
            for blur in covered:
                x, y, width, height = _overlay_pixels(
                    blur, output_width, output_height
                )
                enable = _range_enable(blur.start_us, blur.end_us, start_us, end_us)
                cover_filters.append(
                    f"drawbox=x={x}:y={y}:w={width}:h={height}:"
                    f"color=black@0.900000:t=fill:enable='{enable}'"
                )
            cover_label = f"vsubblur{group_number}covered"
            graph.append(
                f"[{current_label}]" + ",".join(cover_filters) + f"[{cover_label}]"
            )
            current_label = cover_label
    scene_blur_groups: dict[int, list[RenderSceneText]] = {}
    for cue in active_scene_texts:
        scene_blur_groups.setdefault(cue.blur_radius, []).append(cue)
    for group_number, (requested_radius, group) in enumerate(
        sorted(scene_blur_groups.items())
    ):
        base_label = f"vscene{group_number}base"
        source_label = f"vscene{group_number}src"
        graph.append(f"[{current_label}]split=2[{base_label}][{source_label}]")
        radius = max(1, min(requested_radius, output_width // 2, output_height // 2))
        chroma_radius = max(1, min(6, radius // 2))
        blurred_label = f"vscene{group_number}full"
        graph.append(
            f"[{source_label}]boxblur=luma_radius={radius}:luma_power=2:"
            f"chroma_radius={chroma_radius}:chroma_power=2[{blurred_label}]"
        )
        mask_filters = [
            f"color=c=black:s={output_width}x{output_height}:r={snapshot.settings.fps}:"
            f"d={duration_s}"
        ]
        for cue in group:
            x, y, width, height = _scene_glyph_pixels(cue, output_width, output_height)
            enable = _range_enable(cue.start_us, cue.end_us, start_us, end_us)
            mask_filters.append(
                f"drawbox=x={x}:y={y}:w={width}:h={height}:"
                f"color=white@1.000000:t=fill:enable='{enable}'"
            )
        mask_label = f"vscene{group_number}mask"
        graph.append(",".join(mask_filters) + f",format=gray[{mask_label}]")
        next_label = f"vscene{group_number}merged"
        graph.append(
            f"[{base_label}][{blurred_label}][{mask_label}]maskedmerge[{next_label}]"
        )
        current_label = next_label
        fills = []
        for cue in group:
            if not cue.background_color or cue.background_style != "solid":
                continue
            x, y, width, height = _scene_label_background_pixels(
                cue,
                output_width,
                output_height,
            )
            enable = _range_enable(cue.start_us, cue.end_us, start_us, end_us)
            fills.append(
                f"drawbox=x={x}:y={y}:w={width}:h={height}:"
                f"color=0x{cue.background_color[1:]}@"
                f"{cue.background_opacity_percent / 100:.6f}:t=fill:enable='{enable}'"
            )
        if fills:
            filled_label = f"vscene{group_number}filled"
            graph.append(f"[{current_label}]" + ",".join(fills) + f"[{filled_label}]")
            current_label = filled_label
    if blurs:
        split_labels = ["vmain", *(f"vblur{number}src" for number in range(len(blurs)))]
        graph.append(
            f"[{current_label}]split={len(split_labels)}"
            + "".join(f"[{label}]" for label in split_labels)
        )
        current_label = "vmain"
        for number, blur in enumerate(blurs):
            x, y, width, height = _overlay_pixels(blur, output_width, output_height)
            radius = max(1, min(blur.blur_radius, width // 2, height // 2))
            chroma_radius = max(1, min(6, radius // 2))
            graph.append(
                f"[vblur{number}src]crop={width}:{height}:{x}:{y},"
                f"boxblur=luma_radius={radius}:luma_power=1:"
                f"chroma_radius={chroma_radius}:chroma_power=1[vblur{number}]"
            )
            next_label = f"vfx{number}"
            enable = _enable_expression(blur, start_us, end_us)
            graph.append(
                f"[{current_label}][vblur{number}]overlay={x}:{y}:enable='{enable}'"
                f"[{next_label}]"
            )
            current_label = next_label

    for number, (input_index, logo) in enumerate(logo_inputs):
        x, y, width, height = _overlay_pixels(logo, output_width, output_height)
        graph.append(
            f"[{input_index}:v]scale={width}:{height}:flags=lanczos,format=rgba,"
            f"colorchannelmixer=aa={logo.opacity:.6f}[vlogo{number}]"
        )
        next_label = f"vlogoout{number}"
        enable = _enable_expression(logo, start_us, end_us)
        graph.append(
            f"[{current_label}][vlogo{number}]overlay={x}:{y}:eof_action=repeat:"
            f"enable='{enable}'[{next_label}]"
        )
        current_label = next_label

    ass_path: Path | None = None
    active_subtitles = [
        cue
        for cue in snapshot.subtitles
        if cue.end_us > start_us and cue.start_us < end_us
    ]
    if snapshot.settings.subtitle_style.enabled and (active_subtitles or active_scene_texts):
        ass_path = work_dir / f"subtitles-{index:05d}.ass"
        _write_ass(
            ass_path,
            active_subtitles,
            chunk_start_us=start_us,
            chunk_end_us=end_us,
            width=output_width,
            height=output_height,
            style=snapshot.settings.subtitle_style,
            scene_texts=active_scene_texts,
        )
        next_label = "vsub"
        font_directory = _bundled_font_directory()
        font_option = (
            f":fontsdir='{_ffmpeg_filter_path(font_directory)}'"
            if font_directory is not None
            else ""
        )
        graph.append(
            f"[{current_label}]ass=filename='{ass_path.name}'{font_option}[{next_label}]"
        )
        current_label = next_label
    graph.append(f"[{current_label}]null[vout]")

    audio_labels: list[str] = []
    for number, (
        input_index,
        clip,
        intersection_start,
        intersection_end,
        source_start,
        source_duration,
        input_anchor_us,
        input_pretrimmed,
    ) in enumerate(audio_inputs):
        speed = clip.speed_numerator / clip.speed_denominator
        delay_samples = max(
            0,
            round(
                (intersection_start - start_us)
                * EXPORT_AUDIO_SAMPLE_RATE
                / 1_000_000
            ),
        )
        expected_duration = _seconds(intersection_end - intersection_start)
        conformed_original = bool(
            conform_pieces and clip.track_kind == "original_audio"
        )
        input_label = f"{input_index}:a"
        if conformed_original:
            audio_pieces = _chunk_conform_pieces(
                snapshot, intersection_start, intersection_end
            )
            if not audio_pieces:
                raise ValueError("Conformed audio chunk has no time-map segment")
            source_anchor = _conform_output_to_source(snapshot, clip.timeline_start_us)
            piece_labels = []
            for piece_number, (piece_start, piece_end, piece_speed) in enumerate(
                audio_pieces
            ):
                clip_source_start = clip.source_start_us + int(
                    round((piece_start - source_anchor) * speed)
                )
                clip_source_end = clip.source_start_us + int(
                    round((piece_end - source_anchor) * speed)
                )
                piece_label = f"aconform{number}_{piece_number}"
                piece_filters = [
                    f"atrim=start={_seconds(clip_source_start)}:end={_seconds(clip_source_end)}",
                    "asetpts=PTS-STARTPTS",
                    *_atempo_filters(speed * piece_speed),
                ]
                graph.append(
                    f"[{input_index}:a]" + ",".join(piece_filters) + f"[{piece_label}]"
                )
                piece_labels.append(f"[{piece_label}]")
            joined_label = f"aconformed{number}"
            graph.append(
                "".join(piece_labels)
                + f"concat=n={len(piece_labels)}:v=0:a=1[{joined_label}]"
            )
            input_label = joined_label
            filters = []
        else:
            filters = []
            if not input_pretrimmed:
                relative_start = source_start - input_anchor_us
                relative_end = relative_start + source_duration
                filters.append(
                    f"atrim=start={_seconds(relative_start)}:end={_seconds(relative_end)}"
                )
            filters.extend(["asetpts=PTS-STARTPTS", *_atempo_filters(speed)])
        filters.extend(
            [
                f"volume={clip.volume:.8f}",
                f"atrim=duration={expected_duration}",
                f"aresample={EXPORT_AUDIO_SAMPLE_RATE}",
                f"adelay={delay_samples}S:all=1",
            ]
        )
        label = f"audio{number}"
        graph.append(f"[{input_label}]" + ",".join(filters) + f"[{label}]")
        audio_labels.append(f"[{label}]")
    if silent_input is not None:
        graph.append(
            f"[{silent_input}:a]atrim=duration={duration_s},asetpts=PTS-STARTPTS[aout]"
        )
    else:
        mix_filters = [
            f"amix=inputs={len(audio_labels)}:duration=longest:normalize=0"
        ]
        if len(audio_labels) > 1:
            # FFmpeg enables alimiter auto-level by default, which raises the
            # limited mix back toward 0 dBFS. Keep the intended headroom and
            # compensate the look-ahead delay so audio stays sample-aligned.
            mix_filters.append(
                f"alimiter=limit={MIX_PEAK_LIMIT:.8f}:level=0:latency=1"
            )
        # Padding each delayed cue before amix makes FFmpeg discard its leading
        # timeline gap, collapsing every cue to the start of the chunk. Mix the
        # finite cues first, then pad the completed timeline exactly once.
        mix_filters.extend(
            [
                f"apad=whole_dur={duration_s}",
                f"atrim=duration={duration_s}",
            ]
        )
        graph.append(
            "".join(audio_labels) + ",".join(mix_filters) + "[aout]"
        )

    filter_script = work_dir / f"filter-{index:05d}.txt"
    filter_script.write_text(";\n".join(graph), encoding="utf-8", newline="\n")
    backend = _select_backend(snapshot.settings.backend, capabilities)
    args += [
        "-filter_complex_script",
        str(filter_script),
        "-map",
        "[vout]",
        "-map",
        "[aout]",
        "-t",
        duration_s,
        *_video_encoder_arguments(backend, snapshot.settings),
        "-c:a",
        "aac",
        "-b:a",
        snapshot.settings.audio_bitrate,
        "-ar",
        str(EXPORT_AUDIO_SAMPLE_RATE),
        "-max_muxing_queue_size",
        "4096",
        "-progress",
        "pipe:1",
        "-nostats",
        str(output_path),
    ]
    return ExportChunkPlan(
        index,
        start_us,
        end_us,
        output_path,
        ass_path,
        filter_script,
        tuple(args),
        backend,
    )


def _can_reuse_video_input_for_audio(
    *,
    video: RenderClip,
    audio: RenderClip,
    chunk_start_us: int,
    chunk_end_us: int,
    video_source_start_us: int,
    video_source_duration_us: int,
    audio_source_start_us: int,
    audio_source_duration_us: int,
) -> bool:
    return (
        os.path.normcase(os.path.abspath(video.path))
        == os.path.normcase(os.path.abspath(audio.path))
        and audio.timeline_start_us <= chunk_start_us
        and audio.timeline_end_us >= chunk_end_us
        and audio.speed_numerator * video.speed_denominator
        == video.speed_numerator * audio.speed_denominator
        and audio_source_start_us == video_source_start_us
        and audio_source_duration_us == video_source_duration_us
    )


def _chunk_conform_pieces(
    snapshot: ExportSnapshot,
    output_start_us: int,
    output_end_us: int,
) -> tuple[tuple[int, int, float], ...]:
    """Return source-relative pieces intersecting an output-time interval.

    Conform segments are the single source of truth for the output timeline.
    Mapping both endpoints with integer rounding keeps adjacent pieces
    contiguous while preserving each segment's playback rate.
    """

    if not snapshot.conform_segments:
        return ()
    if output_end_us <= output_start_us:
        return ()
    pieces: list[tuple[int, int, float]] = []
    for segment in snapshot.conform_segments:
        start = max(output_start_us, segment.output_start_us)
        end = min(output_end_us, segment.output_end_us)
        if end <= start:
            continue
        source_duration = segment.source_end_us - segment.source_start_us
        output_duration = segment.output_end_us - segment.output_start_us
        source_start = segment.source_start_us + int(
            round((start - segment.output_start_us) * source_duration / output_duration)
        )
        source_end = segment.source_start_us + int(
            round((end - segment.output_start_us) * source_duration / output_duration)
        )
        if source_end > source_start:
            pieces.append((source_start, source_end, segment.video_speed))
    return tuple(pieces)


def _conform_output_to_source(snapshot: ExportSnapshot, output_us: int) -> int:
    """Map an output-timeline position to a source-relative position."""

    if not snapshot.conform_segments:
        return max(0, int(output_us - snapshot.timeline_start_us))
    position = max(snapshot.timeline_start_us, min(snapshot.timeline_end_us, int(output_us)))
    if position == snapshot.timeline_end_us:
        return snapshot.video.source_end_us - snapshot.video.source_start_us
    for segment in snapshot.conform_segments:
        if segment.output_start_us <= position < segment.output_end_us:
            output_duration = segment.output_end_us - segment.output_start_us
            source_duration = segment.source_end_us - segment.source_start_us
            return segment.source_start_us + int(
                round((position - segment.output_start_us) * source_duration / output_duration)
            )
    raise ValueError("Output time is outside the conform time map")


def decode_export_snapshot(raw: object) -> ExportSnapshot:
    if not isinstance(raw, dict):
        raise TypeError("Export snapshot must be an object")
    settings_raw = _dict(raw.get("settings"), "settings")
    subtitle_raw = _dict(settings_raw.get("subtitle_style"), "subtitle_style")
    video_filter_raw = _dict(settings_raw.get("video_filter", {}), "video_filter")
    settings = ExportSettings(
        preset_id=str(settings_raw.get("preset_id", "source-quality")),
        backend=str(settings_raw.get("backend", "auto")),
        video_bitrate=str(settings_raw.get("video_bitrate", "6M")),
        audio_bitrate=str(settings_raw.get("audio_bitrate", "256k")),
        output_width=_optional_int(settings_raw.get("output_width")),
        output_height=_optional_int(settings_raw.get("output_height")),
        fps=int(settings_raw.get("fps", 30)),
        chunk_duration_us=int(settings_raw.get("chunk_duration_us", 600_000_000)),
        overwrite=settings_raw.get("overwrite") is True,
        subtitle_style=SubtitleStyle(**subtitle_raw),
        video_filter=VideoFilterSettings(**video_filter_raw),
    )
    return ExportSnapshot(
        int(raw["schema_version"]),
        str(raw["project_id"]),
        int(raw["project_revision"]),
        int(raw["timeline_start_us"]),
        int(raw["timeline_end_us"]),
        int(raw["source_width"]),
        int(raw["source_height"]),
        RenderClip(**_dict(raw["video"], "video")),
        tuple(RenderClip(**_dict(item, "audio item")) for item in _list(raw["audio"], "audio")),
        tuple(
            RenderOverlay(**_dict(item, "overlay item"))
            for item in _list(raw["overlays"], "overlays")
        ),
        tuple(
            RenderSubtitle(**_dict(item, "subtitle item"))
            for item in _list(raw["subtitles"], "subtitles")
        ),
        settings,
        tuple(
            RenderConformSegment(**_dict(item, "conform segment"))
            for item in _list(raw.get("conform_segments", []), "conform_segments")
        ),
        tuple(
            RenderSubtitleBlur(**_dict(item, "subtitle blur"))
            for item in _list(raw.get("subtitle_blurs", []), "subtitle_blurs")
        ),
        tuple(
            RenderSceneText(**_dict(item, "scene text"))
            for item in _list(raw.get("scene_texts", []), "scene_texts")
        ),
    )


def _select_backend(requested: str, capabilities: FfmpegCapabilities) -> str:
    if requested == "nvenc" and not capabilities.h264_nvenc:
        raise RuntimeError("NVENC was requested but the encoder probe failed")
    if requested == "auto":
        return "nvenc" if capabilities.h264_nvenc else "cpu"
    return requested


def _video_encoder_arguments(backend: str, settings: ExportSettings) -> list[str]:
    if backend == "nvenc":
        return [
            "-c:v",
            "h264_nvenc",
            "-preset",
            "p4",
            "-rc",
            "vbr",
            "-cq",
            "20",
            "-b:v",
            settings.video_bitrate,
        ]
    return [
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-b:v",
        settings.video_bitrate,
    ]


def _probe_nvenc(executable: str) -> bool:
    result = subprocess.run(
        [
            executable,
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=black:s=320x180:r=1",
            "-frames:v",
            "1",
            "-pix_fmt",
            "yuv420p",
            "-c:v",
            "h264_nvenc",
            "-f",
            "null",
            "-",
        ],
        capture_output=True,
        timeout=15,
        check=False,
        **_hidden_subprocess_kwargs(),
    )
    return result.returncode == 0


def _atempo_filters(speed: float) -> list[str]:
    if not math.isfinite(speed) or speed <= 0:
        raise ValueError("Audio speed must be positive")
    if abs(speed - 1.0) < 1e-9:
        return []
    result: list[str] = []
    remaining = speed
    while remaining > 2.0:
        result.append("atempo=2.0")
        remaining /= 2.0
    while remaining < 0.5:
        result.append("atempo=0.5")
        remaining /= 0.5
    result.append(f"atempo={remaining:.10f}")
    return result


def _overlay_pixels(
    overlay: RenderOverlay | RenderSubtitleBlur,
    output_width: int,
    output_height: int,
) -> tuple[int, int, int, int]:
    x = max(0, min(output_width - 2, int(round(overlay.x * output_width))))
    y = max(0, min(output_height - 2, int(round(overlay.y * output_height))))
    width = max(2, min(output_width - x, int(round(overlay.width * output_width))))
    height = max(2, min(output_height - y, int(round(overlay.height * output_height))))
    width -= width % 2
    height -= height % 2
    return x, y, max(2, width), max(2, height)


def _scene_glyph_pixels(
    cue: RenderSceneText,
    output_width: int,
    output_height: int,
) -> tuple[int, int, int, int]:
    proxy = RenderSubtitleBlur(
        cue.cue_id,
        cue.index,
        cue.start_us,
        cue.end_us,
        cue.glyph_x,
        cue.glyph_y,
        cue.glyph_width,
        cue.glyph_height,
    )
    return _overlay_pixels(proxy, output_width, output_height)


def _scene_label_background_pixels(
    cue: RenderSceneText,
    output_width: int,
    output_height: int,
) -> tuple[int, int, int, int]:
    """Approximate Qt-painted bounds for a tight export-side label panel."""

    lines = [line for line in cue.text.splitlines() if line] or [cue.text]
    longest = max(1, max(len(line) for line in lines))
    script_font = cue.style_hint.startswith("calligraphy")
    width_ratio = 0.72 if script_font else 0.62
    line_height_ratio = 1.18 if script_font else 1.10
    spacing = cue.font_size * cue.letter_spacing_percent / 100
    text_width = max(
        cue.font_size,
        round(longest * cue.font_size * width_ratio + max(0, longest - 1) * spacing),
    )
    text_height = max(
        cue.font_size,
        round(len(lines) * cue.font_size * line_height_ratio),
    )
    pad_x = round(cue.font_size * cue.background_padding_x / 100)
    pad_y = round(cue.font_size * cue.background_padding_y / 100)
    width = min(output_width, max(2, text_width + pad_x * 2))
    height = min(output_height, max(2, text_height + pad_y * 2))
    center_x = (cue.panel_x + cue.panel_width / 2) * output_width
    center_y = (cue.panel_y + cue.panel_height / 2) * output_height
    x = max(0, min(output_width - width, round(center_x - width / 2)))
    y = max(0, min(output_height - height, round(center_y - height / 2)))
    return x, y, width, height


def _scene_ink_background_pixels(
    cue: RenderSceneText,
    output_width: int,
    output_height: int,
) -> tuple[int, int, int, int]:
    """Expand the tight text panel enough for organic brush fringes and splats."""

    x, y, width, height = _scene_label_background_pixels(
        cue, output_width, output_height
    )
    extra_x = max(3, round(cue.font_size * (0.34 if cue.orientation == "vertical" else 0.22)))
    extra_y = max(3, round(cue.font_size * (0.22 if cue.orientation == "vertical" else 0.34)))
    left = max(0, x - extra_x)
    top = max(0, y - extra_y)
    right = min(output_width, x + width + extra_x)
    bottom = min(output_height, y + height + extra_y)
    return left, top, max(2, right - left), max(2, bottom - top)


_INK_SHAPES = {
    "ink_splash": (
        0.72,
        (
            (0.46, 0.01), (0.63, 0.04), (0.58, 0.09), (0.79, 0.12),
            (0.68, 0.17), (0.88, 0.22), (0.75, 0.27), (0.92, 0.34),
            (0.77, 0.39), (0.89, 0.45), (0.73, 0.50), (0.86, 0.57),
            (0.74, 0.62), (0.91, 0.69), (0.75, 0.74), (0.83, 0.81),
            (0.63, 0.84), (0.70, 0.92), (0.55, 0.89), (0.61, 0.99),
            (0.43, 0.95), (0.33, 1.00), (0.30, 0.90), (0.12, 0.85),
            (0.25, 0.79), (0.07, 0.73), (0.23, 0.67), (0.10, 0.60),
            (0.25, 0.54), (0.08, 0.48), (0.27, 0.42), (0.12, 0.35),
            (0.29, 0.30), (0.09, 0.23), (0.31, 0.18), (0.22, 0.10),
            (0.41, 0.08),
        ),
        (
            (0.08, 0.16, 0.025, 0.016),
            (0.94, 0.29, 0.018, 0.012),
            (0.04, 0.64, 0.020, 0.013),
            (0.91, 0.82, 0.028, 0.018),
        ),
    ),
    "ink_wash": (
        0.82,
        (
            (0.43, 0.00), (0.62, 0.02), (0.58, 0.06), (0.78, 0.08),
            (0.70, 0.12), (0.96, 0.16), (0.76, 0.20), (0.88, 0.25),
            (0.74, 0.30), (0.93, 0.35), (0.80, 0.40), (0.89, 0.46),
            (0.73, 0.51), (0.95, 0.57), (0.78, 0.62), (0.88, 0.68),
            (0.76, 0.74), (0.91, 0.80), (0.72, 0.85), (0.78, 0.91),
            (0.61, 0.94), (0.56, 1.00), (0.39, 0.97), (0.28, 0.93),
            (0.32, 0.88), (0.11, 0.84), (0.23, 0.78), (0.06, 0.73),
            (0.24, 0.67), (0.10, 0.61), (0.26, 0.56), (0.05, 0.50),
            (0.25, 0.45), (0.12, 0.39), (0.28, 0.34), (0.07, 0.28),
            (0.27, 0.23), (0.04, 0.18), (0.29, 0.13), (0.17, 0.08),
            (0.38, 0.06),
        ),
        (
            (0.07, 0.11, 0.032, 0.020),
            (0.95, 0.22, 0.026, 0.018),
            (0.03, 0.64, 0.020, 0.014),
            (0.92, 0.72, 0.036, 0.024),
            (0.16, 0.91, 0.025, 0.018),
            (0.84, 0.06, 0.018, 0.013),
        ),
    ),
    "ink_cloud": (
        0.96,
        (
            (0.38, 0.02), (0.56, 0.00), (0.70, 0.05), (0.78, 0.11),
            (0.91, 0.17), (0.84, 0.24), (0.94, 0.31), (0.85, 0.38),
            (0.92, 0.46), (0.84, 0.54), (0.94, 0.62), (0.83, 0.69),
            (0.89, 0.77), (0.77, 0.84), (0.73, 0.92), (0.58, 0.98),
            (0.43, 1.00), (0.30, 0.95), (0.22, 0.89), (0.24, 0.82),
            (0.10, 0.76), (0.17, 0.68), (0.07, 0.61), (0.16, 0.53),
            (0.06, 0.45), (0.17, 0.37), (0.09, 0.29), (0.20, 0.22),
            (0.17, 0.14), (0.30, 0.09),
        ),
        (
            (0.11, 0.27, 0.022, 0.016),
            (0.91, 0.43, 0.018, 0.013),
            (0.13, 0.72, 0.017, 0.012),
        ),
    ),
    "brush_stroke": (
        0.62,
        (
            (0.36, 0.01), (0.52, 0.04), (0.66, 0.00), (0.75, 0.06),
            (0.70, 0.12), (0.83, 0.19), (0.78, 0.29), (0.86, 0.40),
            (0.80, 0.50), (0.85, 0.61), (0.77, 0.72), (0.82, 0.82),
            (0.69, 0.91), (0.62, 1.00), (0.54, 0.94), (0.45, 1.00),
            (0.40, 0.93), (0.27, 0.88), (0.22, 0.78), (0.18, 0.67),
            (0.23, 0.56), (0.16, 0.45), (0.22, 0.34), (0.17, 0.24),
            (0.28, 0.15), (0.24, 0.08),
        ),
        (
            (0.12, 0.18, 0.016, 0.010),
            (0.88, 0.83, 0.018, 0.011),
        ),
    ),
}


def _ink_splash_ass_drawing(
    cue: RenderSceneText,
    output_width: int,
    output_height: int,
) -> str:
    """Create a deterministic, aspect-safe ASS vector ink silhouette."""

    x, y, width, height = _scene_ink_background_pixels(
        cue, output_width, output_height
    )

    cap_scale, body_points, splat_points = _INK_SHAPES.get(
        cue.background_style, _INK_SHAPES["ink_splash"]
    )
    vertical = cue.orientation == "vertical"
    thickness = width if vertical else height
    length = height if vertical else width

    def long_coordinate(value: float) -> float:
        edge = 0.24
        cap = min(length * 0.34, thickness * cap_scale)
        if value <= edge:
            return value / edge * cap
        if value >= 1.0 - edge:
            return length - (1.0 - value) / edge * cap
        return cap + (value - edge) / (1.0 - edge * 2.0) * max(
            0.0, length - cap * 2.0
        )

    def orient(u: float, v: float) -> tuple[float, float]:
        cross = u * thickness
        along = long_coordinate(v)
        local_x, local_y = (
            (cross, along) if vertical else (along, thickness - cross)
        )
        if cue.index % 2 == 0:
            local_x = width - local_x
        return x + local_x, y + local_y

    points = [orient(u, v) for u, v in body_points]
    start = (
        (points[-1][0] + points[0][0]) / 2.0,
        (points[-1][1] + points[0][1]) / 2.0,
    )
    body_parts = [f"m {round(start[0])} {round(start[1])}"]
    cursor_x, cursor_y = start
    for index, (control_x, control_y) in enumerate(points):
        next_x, next_y = points[(index + 1) % len(points)]
        end_x = (control_x + next_x) / 2.0
        end_y = (control_y + next_y) / 2.0
        first_x = cursor_x + (control_x - cursor_x) * 2.0 / 3.0
        first_y = cursor_y + (control_y - cursor_y) * 2.0 / 3.0
        second_x = end_x + (control_x - end_x) * 2.0 / 3.0
        second_y = end_y + (control_y - end_y) * 2.0 / 3.0
        body_parts.append(
            "b "
            f"{round(first_x)} {round(first_y)} "
            f"{round(second_x)} {round(second_y)} "
            f"{round(end_x)} {round(end_y)}"
        )
        cursor_x, cursor_y = end_x, end_y
    body = " ".join(body_parts)
    splats = []
    for u, v, cross_scale, long_scale in splat_points:
        center_x, center_y = orient(u, v)
        cross_radius = max(1.0, thickness * cross_scale)
        long_radius = max(1.0, thickness * long_scale)
        radius_x, radius_y = (
            (cross_radius, long_radius)
            if vertical
            else (long_radius, cross_radius)
        )
        splats.append(
            "m "
            f"{round(center_x - radius_x)} {round(center_y)} "
            f"b {round(center_x - radius_x)} {round(center_y - radius_y)} "
            f"{round(center_x + radius_x)} {round(center_y - radius_y)} "
            f"{round(center_x + radius_x)} {round(center_y)} "
            f"b {round(center_x + radius_x)} {round(center_y + radius_y)} "
            f"{round(center_x - radius_x)} {round(center_y + radius_y)} "
            f"{round(center_x - radius_x)} {round(center_y)}"
        )
    return " ".join((body, *splats))


def _bundled_font_directory() -> Path | None:
    source = Path(__file__).resolve().parents[2] / "ui" / "assets" / "fonts"
    if source.is_dir():
        return source
    frozen = Path(sys.executable).resolve().parent / "ui" / "assets" / "fonts"
    return frozen if frozen.is_dir() else None


def _ffmpeg_filter_path(path: Path) -> str:
    value = path.resolve().as_posix()
    if len(value) >= 2 and value[1] == ":":
        value = value[0] + r"\:" + value[2:]
    return value.replace("'", r"\'")


def _enable_expression(overlay: RenderOverlay, chunk_start: int, chunk_end: int) -> str:
    return _range_enable(overlay.start_us, overlay.end_us, chunk_start, chunk_end)


def _range_enable(
    item_start_us: int,
    item_end_us: int,
    chunk_start: int,
    chunk_end: int,
) -> str:
    start = max(chunk_start, item_start_us) - chunk_start
    end = min(chunk_end, item_end_us) - chunk_start
    # Project ranges are half-open.  ``between`` includes both endpoints and
    # therefore concealed one extra frame at a cue boundary.
    return (
        f"gte(t,{start / 1_000_000:.6f})"
        f"*lt(t,{end / 1_000_000:.6f})"
    )


def _write_ass(
    path: Path,
    cues: list[RenderSubtitle],
    *,
    chunk_start_us: int,
    chunk_end_us: int,
    width: int,
    height: int,
    style: SubtitleStyle,
    scene_texts: list[RenderSceneText] | None = None,
) -> None:
    style_format = (
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
        "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, "
        "ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, "
        "MarginR, MarginV, Encoding"
    )
    style_value = (
        f"Style: Default,{style.font_family},{style.font_size},"
        f"{_ass_color(style.primary_color, style.opacity_percent)},&H000000FF,"
        f"{_ass_color(style.outline_color)},&H64000000,0,0,0,0,100,100,0,0,1,"
        f"{style.outline_width},0,{style.alignment},40,40,{style.margin_bottom},1"
    )
    background_style_names = {
        (color, opacity): f"CueBackground{index}"
        for index, (color, opacity) in enumerate(
            sorted(
                {
                    (cue.background_color, cue.background_opacity_percent)
                    for cue in cues
                    if cue.background_color
                }
            ),
            start=1,
        )
    }
    style_rows = [style_value]
    for (color, opacity), name in background_style_names.items():
        background = _ass_color(color, opacity)
        style_rows.append(
            f"Style: {name},{style.font_family},{style.font_size},"
            f"{_ass_color(style.primary_color, style.opacity_percent)},&H000000FF,"
            f"{background},{background},0,0,0,0,100,100,0,0,3,"
            f"6,0,{style.alignment},40,40,{style.margin_bottom},1"
        )
    header = (
        "[Script Info]\n"
        "ScriptType: v4.00+\n"
        f"PlayResX: {width}\n"
        f"PlayResY: {height}\n"
        "WrapStyle: 0\n"
        "ScaledBorderAndShadow: yes\n\n"
        "[V4+ Styles]\n"
        f"{style_format}\n"
        + "\n".join(style_rows)
        + "\n\n"
        + "[Events]\n"
        + "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, "
        + "Effect, Text\n"
    )
    lines = [header]
    for cue in scene_texts or []:
        start = max(chunk_start_us, cue.start_us) - chunk_start_us
        end = min(chunk_end_us, cue.end_us) - chunk_start_us
        if end <= start:
            continue
        fade_ms = min(
            cue.fade_duration_ms,
            max(0, (cue.end_us - cue.start_us) // 2_000),
        ) if cue.fade_enabled else 0
        fade_in_ms = fade_ms if cue.start_us >= chunk_start_us else 0
        fade_out_ms = fade_ms if cue.end_us <= chunk_end_us else 0
        fade_override = (
            f"\\fad({fade_in_ms},{fade_out_ms})"
            if fade_in_ms or fade_out_ms
            else ""
        )
        if cue.background_style in _INK_SHAPES:
            drawing = _ink_splash_ass_drawing(cue, width, height)
            ink_color = cue.background_color or "#080808"
            ink_override = (
                r"{\an7\pos(0,0)\p1\bord0\shad0"
                + _ass_primary_override(ink_color)
                + _ass_alpha_override(cue.background_opacity_percent)
                + fade_override
                + "}"
            )
            lines.append(
                f"Dialogue: 0,{_ass_time(start)},{_ass_time(end)},Default,,0,0,0,,"
                f"{ink_override}{drawing}{{\\p0}}\n"
            )
        position_x = round((cue.panel_x + cue.panel_width / 2) * width)
        position_y = round((cue.panel_y + cue.panel_height / 2) * height)
        text = _ass_text(cue.text)
        override = (
            r"{\an5\q2"
            + f"\\pos({position_x},{position_y})"
            + f"\\fn{_ass_override_text(cue.font_family)}"
            + f"\\fs{cue.font_size}"
            + (_ass_primary_override(cue.text_color) if cue.text_color else "")
            + (f"\\fsp{round(cue.font_size * cue.letter_spacing_percent / 100)}")
            + (r"\b1" if cue.font_bold else r"\b0")
            + (r"\i1" if cue.font_italic else r"\i0")
            + _ass_global_alpha_override(cue.text_opacity_percent)
            + fade_override
            + r"\bord2\shad1}"
        )
        lines.append(
            f"Dialogue: 1,{_ass_time(start)},{_ass_time(end)},Default,,0,0,0,,"
            f"{override}{text}\n"
        )
    for cue in cues:
        start = max(chunk_start_us, cue.start_us) - chunk_start_us
        end = min(chunk_end_us, cue.end_us) - chunk_start_us
        if end <= start:
            continue
        text = _ass_text(cue.text)
        cue_style = background_style_names.get(
            (cue.background_color, cue.background_opacity_percent), "Default"
        )
        override_values = []
        if cue.font_size > 0:
            override_values.append(f"\\fs{cue.font_size}")
        if cue.text_color:
            override_values.append(_ass_primary_override(cue.text_color))
        if cue.font_family:
            override_values.append(f"\\fn{_ass_override_text(cue.font_family)}")
        override_values.append(
            f"\\fsp{round(cue.font_size * cue.letter_spacing_percent / 100)}"
        )
        override_values.append(r"\b1" if cue.font_bold else r"\b0")
        override_values.append(r"\i1" if cue.font_italic else r"\i0")
        override_values.append(_ass_alpha_override(cue.text_opacity_percent))
        if cue.panel_x >= 0:
            position_x = round((cue.panel_x + cue.panel_width / 2) * width)
            position_y = round((cue.panel_y + cue.panel_height / 2) * height)
            override_values.extend(
                (
                    r"\an5\q2",
                    f"\\pos({position_x},{position_y})",
                )
            )
        override = "{" + "".join(override_values) + "}" if override_values else ""
        lines.append(
            f"Dialogue: 2,{_ass_time(start)},{_ass_time(end)},{cue_style},,0,0,0,,"
            f"{override}{text}\n"
        )
    path.write_text("".join(lines), encoding="utf-8-sig", newline="\n")


def _ass_color(value: str, opacity_percent: int = 100) -> str:
    red = value[1:3]
    green = value[3:5]
    blue = value[5:7]
    alpha = round((100 - opacity_percent) * 255 / 100)
    return f"&H{alpha:02X}{blue}{green}{red}"


def _ass_primary_override(value: str) -> str:
    red = value[1:3]
    green = value[3:5]
    blue = value[5:7]
    return f"\\c&H{blue}{green}{red}&"


def _ass_alpha_override(opacity_percent: int) -> str:
    alpha = round((100 - opacity_percent) * 255 / 100)
    return f"\\1a&H{alpha:02X}&"


def _ass_global_alpha_override(opacity_percent: int) -> str:
    alpha = round((100 - opacity_percent) * 255 / 100)
    return f"\\alpha&H{alpha:02X}&"


def _ass_override_text(value: str) -> str:
    return value.replace("\\", "").replace("{", "").replace("}", "")


def _video_filter_chain(settings: VideoFilterSettings) -> list[str]:
    intensity = settings.intensity_percent / 100
    brightness = settings.brightness_percent / 100
    contrast = settings.contrast_percent / 100
    saturation = settings.saturation_percent / 100
    filters: list[str] = []
    if settings.preset_id == "cinematic-warm":
        contrast *= 1 + 0.08 * intensity
        saturation *= 1 + 0.12 * intensity
        filters.append(
            "colorbalance="
            f"rs={0.08 * intensity:.4f}:gs={0.025 * intensity:.4f}:"
            f"bs={-0.06 * intensity:.4f}"
        )
    elif settings.preset_id == "cool":
        contrast *= 1 + 0.05 * intensity
        filters.append(
            "colorbalance="
            f"rs={-0.05 * intensity:.4f}:gs={0.01 * intensity:.4f}:"
            f"bs={0.08 * intensity:.4f}"
        )
    elif settings.preset_id == "grayscale":
        saturation *= 1 - intensity
    if (
        abs(brightness) > 1e-9
        or abs(contrast - 1) > 1e-9
        or abs(saturation - 1) > 1e-9
    ):
        filters.append(
            f"eq=brightness={brightness:.4f}:contrast={contrast:.4f}:"
            f"saturation={saturation:.4f}"
        )
    return filters


def _ass_text(value: str) -> str:
    return (
        value.replace("\\", r"\\")
        .replace("{", r"\{")
        .replace("}", r"\}")
        .replace("\r\n", r"\N")
        .replace("\r", r"\N")
        .replace("\n", r"\N")
    )


def _ass_time(value_us: int) -> str:
    centiseconds = max(0, int(round(value_us / 10_000)))
    hours, remainder = divmod(centiseconds, 360_000)
    minutes, remainder = divmod(remainder, 6_000)
    seconds, cents = divmod(remainder, 100)
    return f"{hours}:{minutes:02d}:{seconds:02d}.{cents:02d}"


def _seconds(value_us: int) -> str:
    return f"{value_us / 1_000_000:.6f}"


def _dict(value: object, name: str) -> dict:
    if not isinstance(value, dict):
        raise TypeError(f"Export {name} must be an object")
    return value


def _list(value: object, name: str) -> list:
    if not isinstance(value, list):
        raise TypeError(f"Export {name} must be an array")
    return value


def _optional_int(value: object) -> int | None:
    return None if value is None else int(value)
