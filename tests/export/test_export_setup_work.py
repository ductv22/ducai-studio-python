"""Large split timelines must not rescan all videos for every source-audio clip."""

from ducai_studio.application.export_pipeline import build_export_snapshot
from ducai_studio.domain.effects import ExportSettings, SubtitleStyle
from ducai_studio.domain.project import (
    MediaAsset,
    MediaKind,
    ProjectSnapshot,
    TimelineClip,
    TimelineTrack,
    TrackKind,
)
from ducai_studio.domain.timebase import TimeRange
from ducai_studio.providers.media_probe import MediaProbeResult


def test_export_link_matching_has_bounded_work_and_preserves_audio_order(tmp_path):
    comparisons = []

    class Link(str):
        __hash__ = str.__hash__

        def __eq__(self, other):
            comparisons.append(1)
            return super().__eq__(other)

    class Probe:
        def probe(self, _path):
            return MediaProbeResult(500_000_000, True, True, 160, 90, "mp4")

    path = tmp_path / "source.mp4"
    path.touch()
    count = 500
    video, audio = [], []
    for i in range(count):
        interval = TimeRange(i * 1_000_000, (i + 1) * 1_000_000)
        video.append(TimelineClip(f"v{i}", "V1", interval, interval, "media",
                                  link_group_id=Link(str(i))))
        audio.append(TimelineClip(f"a{i}", "A1", interval, interval, "media",
                                  link_group_id=Link(str(i))))
    hidden_range = TimeRange(0, 1_000_000)
    hidden = TimelineClip("hidden", "V2", hidden_range, hidden_range, "media",
                          link_group_id="hidden")
    hidden_audio = TimelineClip("hidden-audio", "A1", hidden_range, hidden_range, "media",
                                link_group_id="hidden")
    project = ProjectSnapshot("export-work", "Split timeline", 1,
        media=(MediaAsset("media", str(path), MediaKind.VIDEO, count * 1_000_000),),
        tracks=(TimelineTrack("V1", TrackKind.VIDEO, "Video", tuple(video)),
                TimelineTrack("V2", TrackKind.VIDEO, "Hidden", (hidden,), visible=False),
                TimelineTrack("A1", TrackKind.ORIGINAL_AUDIO, "Original",
                              (*audio, hidden_audio))))
    comparisons.clear()
    result = build_export_snapshot(project,
        settings=ExportSettings(subtitle_style=SubtitleStyle(enabled=False)), media_probe=Probe())
    assert [clip.clip_id for clip in result.audio] == [f"a{i}" for i in range(count)]
    assert [(clip.timeline_start_us, clip.timeline_end_us) for clip in result.audio] == [
        (i * 1_000_000, (i + 1) * 1_000_000) for i in range(count)]
    assert len(comparisons) < count * 10
