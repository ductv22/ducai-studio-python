from ducai_studio.domain.video_layers import resolved_video_rows


def video(identifier, track, start, end, source=0):
    return {
        "id": identifier,
        "track_id": track,
        "media_id": "media",
        "timeline_range": {"start_us": start, "end_us": end},
        "source_range": {"start_us": source, "end_us": source + end - start},
    }


def test_last_visible_track_wins_not_latest_clip_start():
    raw = {
        "tracks": [
            {
                "id": "V1",
                "kind": "video",
                "clips": [video("a", "V1", 0, 5), video("b", "V1", 5, 10, 5)],
            },
            {"id": "V2", "kind": "video", "clips": [video("overlay", "V2", 2, 8, 20)]},
            {
                "id": "V3",
                "kind": "video",
                "visible": False,
                "clips": [video("hidden", "V3", 0, 10)],
            },
        ]
    }
    pieces = resolved_video_rows(raw)
    assert [(x["id"], x["timeline_range"]) for x in pieces] == [
        ("a", {"start_us": 0, "end_us": 2}),
        ("overlay", {"start_us": 2, "end_us": 8}),
        ("b", {"start_us": 8, "end_us": 10}),
    ]
    assert pieces[-1]["source_range"] == {"start_us": 8, "end_us": 10}
    assert [x["id"] for x in resolved_video_rows(raw, last_track_wins=False)] == ["a", "b"]


def test_layer_extension_does_not_duplicate_clock_or_lose_hidden_underlying_clip():
    from ducai_studio.domain.timeline_edit import project_json_for_timeline, timeline_spans

    raw = {
        "tracks": [
            {"id": "V1", "kind": "video", "clips": [video("a", "V1", 0, 10)]},
            {"id": "V2", "kind": "video", "clips": [video("overlay", "V2", 5, 15, 20)]},
        ],
        "timeline_edit": {"retained_ranges": [{"start_us": 2, "end_us": 13}]},
    }
    spans = timeline_spans(raw)
    assert spans[-1]["timeline_end_us"] == 11
    projected = project_json_for_timeline(raw)
    assert projected["tracks"][0]["clips"][0]["timeline_range"] == {"start_us": 0, "end_us": 8}
    assert [x["timeline_range"] for x in projected["tracks"][1]["clips"]] == [
        {"start_us": 3, "end_us": 8},
        {"start_us": 8, "end_us": 11},
    ]
