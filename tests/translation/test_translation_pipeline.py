from __future__ import annotations

import json
from dataclasses import replace

import pytest

from ducai_studio.application.translation_pipeline import (
    ChunkState,
    FilmBible,
    FilmTerm,
    TranslationQaIssue,
    TranslationQaSeverity,
    TranslationValue,
    build_ledger,
    contains_cjk,
    film_bible_request,
    film_bible_translation_qa,
    ledger_from_dict,
    ledger_to_dict,
    local_translation_qa,
    mark_chunk_complete,
    parse_film_bible_response,
    parse_qa_response,
    parse_translation_response,
    plan_chunks,
    qa_request,
    reopen_qa_reviews_for_resume,
    repair_request,
    replan_ledger_for_resume,
    semantic_groups,
    translation_request,
    translation_style_qa,
    wrap_vietnamese_subtitle,
)
from ducai_studio.domain.project import SubtitleCue
from ducai_studio.domain.timebase import TimeRange


def _cues(count: int = 6) -> tuple[SubtitleCue, ...]:
    texts = (
        "我要开始修炼。",
        "然后成仙。",
        "师姐，你怎么来了？",
        "宗门弟子都在等你。",
        "那只妖兽已经逃走了！",
        "我们立刻出发。",
    )
    return tuple(
        SubtitleCue(
            f"cue-{index}",
            index,
            TimeRange((index - 1) * 1_000_000, index * 1_000_000),
            texts[index - 1],
            "zh",
        )
        for index in range(1, count + 1)
    )


def test_cjk_detection_covers_supplementary_han_extensions() -> None:
    assert contains_cjk("Còn sót chữ \U00020000") is True


def test_xianxia_prompt_carries_pronoun_title_and_contextual_alias_rules() -> None:
    chunk = plan_chunks(semantic_groups(_cues(3)))[0]
    bible = FilmBible(
        profile="xianxia",
        style_preset="xianxia",
        custom_rules="Nhân vật chính luôn xưng tại hạ khi gặp người lạ.",
    )

    request = translation_request(chunk, film_bible=bible)

    assert "Tu tiên cổ trang" in request.prompt
    assert "Hồng Ly tỷ tỷ" in request.prompt
    assert "阿狗 → A Cẩu" in request.prompt
    assert "không đổi từ chó/mèo" in request.prompt.casefold()
    assert bible.custom_rules in request.prompt


def test_scene_text_prompt_preserves_visual_role_and_skips_dialogue_length_qa() -> None:
    cue = SubtitleCue(
        "scene-title",
        1,
        TimeRange(0, 800_000),
        "海王兽",
        "zh",
        role="scene_text",
        orientation="vertical",
        style_hint="calligraphy",
        visual_box=(0.72, 0.22, 0.08, 0.31),
        visual_box_source="ocr",
        panel_box=(0.68, 0.18, 0.16, 0.39),
    )
    chunk = plan_chunks(semantic_groups((cue,)))[0]

    request = translation_request(chunk, film_bible=FilmBible())
    issues = local_translation_qa(
        (TranslationValue(cue.id, cue.index, "Hải Vương Thú " * 20),),
        long_line_chars=20,
        scene_text_ids=(cue.index,),
    )

    assert '"role": "scene_text"' in request.prompt
    assert '"orientation": "vertical"' in request.prompt
    assert '"style_hint": "calligraphy"' in request.prompt
    assert "không biến thành câu thoại" in request.prompt
    assert not {"long_line", "overlong_translation"} & {issue.code for issue in issues}


def test_ancient_style_qa_uses_source_context_and_ignores_ordinary_animal_words() -> None:
    cues = (
        SubtitleCue("alias", 1, TimeRange(0, 1_000_000), "阿狗，你过来。", "zh"),
        SubtitleCue("animal", 2, TimeRange(1_000_000, 2_000_000), "这只狗很可爱。", "zh"),
        SubtitleCue("title", 3, TimeRange(2_000_000, 3_000_000), "红璃姐姐。", "zh"),
    )
    chunk = plan_chunks(semantic_groups(cues))[0]
    values = (
        TranslationValue("alias", 1, "A Chó, anh qua đây."),
        TranslationValue("animal", 2, "Con chó này rất đáng yêu."),
        TranslationValue("title", 3, "Tỷ tỷ Hồng Ly."),
    )

    issues = translation_style_qa(
        chunk,
        values,
        film_bible=FilmBible(profile="xianxia", style_preset="xianxia"),
    )

    by_index = {
        index: {item.code for item in issues if item.index == index}
        for index in range(1, 4)
    }
    assert by_index[1] == {"ancient_alias_mismatch", "ancient_modern_pronoun"}
    assert by_index[2] == set()
    assert by_index[3] == {"ancient_title_order"}


def test_ancient_title_qa_does_not_treat_lowercase_words_as_character_names() -> None:
    cue = SubtitleCue("family", 1, TimeRange(0, 1_000_000), "我的姐姐。", "zh")
    chunk = plan_chunks(semantic_groups((cue,)))[0]

    issues = translation_style_qa(
        chunk,
        (TranslationValue("family", 1, "Tỷ tỷ của tôi."),),
        film_bible=FilmBible(profile="xianxia", style_preset="xianxia"),
    )

    assert not {item.code for item in issues} & {"ancient_title_order"}


def test_translation_style_changes_new_ledger_signature_but_legacy_remains_stable() -> None:
    cues = _cues(2)
    chunks = plan_chunks(semantic_groups(cues))
    legacy = build_ledger(cues, chunks, provider_id="fixture", model_id="model")
    ancient = build_ledger(
        cues,
        chunks,
        provider_id="fixture",
        model_id="model",
        film_bible=FilmBible(style_preset="xianxia"),
    )
    modern = build_ledger(
        cues,
        chunks,
        provider_id="fixture",
        model_id="model",
        film_bible=FilmBible(style_preset="modern"),
    )

    assert legacy.signature != ancient.signature
    assert ancient.signature != modern.signature


def _numbered_cues(count: int) -> tuple[SubtitleCue, ...]:
    return tuple(
        SubtitleCue(
            f"cue-{index}",
            index,
            TimeRange((index - 1) * 700_000, index * 700_000),
            f"第{index}句话。",
            "zh",
        )
        for index in range(1, count + 1)
    )


def test_semantic_chunk_plan_never_splits_a_group() -> None:
    cues = _cues()
    groups = semantic_groups(cues, max_group_cues=3)
    chunks = plan_chunks(groups, max_cues=2, max_source_chars=100)

    assert [cue.index for chunk in chunks for cue in chunk.cues] == list(range(1, 7))
    assert all(group in chunk.groups for chunk in chunks for group in chunk.groups)


def test_150_cue_plan_is_not_split_by_the_old_24_cue_default() -> None:
    cues = _numbered_cues(320)
    chunks = plan_chunks(
        semantic_groups(cues),
        max_cues=150,
        max_source_chars=8_000,
    )

    assert [len(chunk.cues) for chunk in chunks] == [150, 150, 20]
    assert chunks[0].ids == tuple(range(1, 151))
    assert chunks[-1].ids == tuple(range(301, 321))


def test_translation_request_carries_context_bible_and_exact_ids() -> None:
    cues = _cues(3)
    chunk = plan_chunks(semantic_groups(cues))[0]
    request = translation_request(
        chunk,
        film_bible=FilmBible(
            title="Tu tiên",
            profile="xianxia",
            terms=(FilmTerm("师姐", "sư tỷ", "name"),),
        ),
        previous=(TranslationValue("prior", 99, "Ta sẽ quay lại."),),
        following=_cues()[3:],
    )

    assert request.expected_ids == (1, 2, 3)
    assert "không thêm tình tiết" in request.prompt
    assert "tự kiểm tra ngay trong cùng lượt" in request.prompt
    assert "sư tỷ" in request.prompt
    assert "Ta sẽ quay lại" in request.prompt
    assert '"slot_duration_ms"' in request.prompt
    assert '"soft_max_chars"' in request.prompt
    assert '"start_us"' in request.prompt
    assert "ngân sách mềm" in request.prompt


def test_all_gemini_request_purposes_expose_explicit_required_json_schema() -> None:
    cues = _cues(2)
    chunk = plan_chunks(semantic_groups(cues))[0]
    bible = FilmBible(title="Tu tiên", profile="xianxia")
    translated = (
        TranslationValue("cue-1", 1, "Ta bắt đầu tu luyện."),
        TranslationValue("cue-2", 2, "Sau đó sẽ thành tiên."),
    )

    translation = translation_request(chunk, film_bible=bible)
    film_bible = film_bible_request(cues, title="Tu tiên")
    qa = qa_request(chunk, translated, film_bible=bible)
    repair = repair_request(chunk, translated, (), film_bible=bible)

    assert set(translation.response_schema["required"]) == {
        "translations",
        "glossary_updates",
        "issues",
    }
    assert "translations" in translation.response_schema["properties"]
    assert "issues" in translation.response_schema["properties"]
    assert '"issues":[]' in translation.prompt
    assert set(repair.response_schema["required"]) == {
        "translations",
        "glossary_updates",
        "issues",
    }
    assert set(film_bible.response_schema["required"]) == {
        "profile",
        "synopsis",
        "characters",
        "terms",
    }
    assert film_bible.response_schema["properties"]["terms"]["type"] == "array"
    assert qa.response_schema["required"] == ("issues",)
    assert qa.response_schema["properties"]["issues"]["type"] == "array"
    assert "ALLOWED_IDS=[1, 2]" in qa.prompt
    assert '"id":1' not in qa.prompt


def test_film_bible_uses_bounded_cross_film_sample_and_strict_terms() -> None:
    cues = tuple(
        SubtitleCue(
            f"cue-{index}",
            index,
            TimeRange(index * 1_000_000, (index + 1) * 1_000_000),
            f"第{index}句话。",
            "zh",
        )
        for index in range(1, 301)
    )
    request = film_bible_request(cues, title="Bộ phim", sample_size=30)
    bible = parse_film_bible_response(
        '{"profile":"xianxia","synopsis":"Tu luyện",'
        '"characters":["Thư Hàn"],"terms":['
        '{"source":"师姐","vi":"sư tỷ","kind":"name"}]}',
        title="Bộ phim",
    )

    assert request.prompt.count('"id"') == 30
    assert bible.profile == "xianxia"
    assert bible.terms == (FilmTerm("师姐", "sư tỷ", "name"),)


def test_film_bible_drops_cjk_from_every_vietnamese_metadata_field() -> None:
    bible = parse_film_bible_response(
        json.dumps(
            {
                "profile": "xianxia",
                "synopsis": "Một câu chuyện về 苏雨萱.",
                "characters": ["Diệp Thần", "Tô Vũ萱"],
                "terms": [
                    {"source": "叶晨", "vi": "Diệp Thần", "kind": "name"},
                    {"source": "苏雨萱", "vi": "Tô Vũ萱", "kind": "name"},
                ],
            },
            ensure_ascii=False,
        )
    )

    assert bible.synopsis == ""
    assert bible.characters == ("Diệp Thần",)
    assert bible.terms == (FilmTerm("叶晨", "Diệp Thần", "name"),)
    with pytest.raises(ValueError, match="must not contain CJK"):
        FilmTerm("苏雨萱", "Tô Vũ萱", "name")


def test_translation_response_rejects_missing_duplicate_or_empty_ids() -> None:
    cues = _cues(2)
    with pytest.raises(ValueError, match="coverage mismatch"):
        parse_translation_response(
            '{"translations":[{"id":1,"text":"Ta bắt đầu tu luyện."}]}',
            expected_cues=cues,
        )
    with pytest.raises(ValueError, match="coverage mismatch"):
        parse_translation_response(
            '{"translations":[{"id":1,"text":"A"},{"id":1,"text":"B"}]}',
            expected_cues=cues,
        )
    with pytest.raises(ValueError, match="empty"):
        parse_translation_response(
            '{"translations":[{"id":1,"text":""},{"id":2,"text":"B"}]}',
            expected_cues=cues,
        )


def test_translation_response_repairs_only_json_syntax_and_keeps_order() -> None:
    cues = _cues(2)
    raw = """```json
    {"translations":[
      {"id":1,"text":"Ta bắt đầu tu luyện."},
      {"id":2,"text":"Sau đó sẽ thành tiên."},
    ],"glossary_updates":[{"source":"修炼","vi":"tu luyện","kind":"term"}],}
    ```"""

    result = parse_translation_response(raw, expected_cues=cues)

    assert [value.index for value in result.values] == [1, 2]
    assert result.glossary_updates == (FilmTerm("修炼", "tu luyện", "term"),)


def test_translation_response_keeps_cues_but_drops_poisoned_glossary_update() -> None:
    result = parse_translation_response(
        json.dumps(
            {
                "translations": [
                    {"id": 1, "text": "Diệp Thần đã đến."},
                    {"id": 2, "text": "Tô Vũ Huyên đang đợi."},
                ],
                "glossary_updates": [{"source": "苏雨萱", "vi": "Tô Vũ萱", "kind": "name"}],
                "issues": [],
            },
            ensure_ascii=False,
        ),
        expected_cues=_cues(2),
    )

    assert [value.text for value in result.values] == [
        "Diệp Thần đã đến.",
        "Tô Vũ Huyên đang đợi.",
    ]
    assert result.glossary_updates == ()


def test_translation_response_keeps_integrated_qa_in_the_same_model_reply() -> None:
    cues = _cues(2)
    raw = json.dumps(
        {
            "translations": [
                {"id": 1, "text": "Ta bắt đầu tu luyện."},
                {"id": 2, "text": "Sau đó sẽ thành tiên."},
            ],
            "glossary_updates": [],
            "issues": [
                {
                    "id": 2,
                    "severity": "blocker",
                    "code": "context",
                    "message": "Câu này còn sai ngữ cảnh.",
                    "suggestion": "Sau đó ta sẽ thành tiên.",
                }
            ],
        },
        ensure_ascii=False,
    )

    result = parse_translation_response(raw, expected_cues=cues)

    assert result.qa_issues == (
        TranslationQaIssue(
            2,
            "context",
            TranslationQaSeverity.BLOCKER,
            "Câu này còn sai ngữ cảnh.",
            "Sau đó ta sẽ thành tiên.",
        ),
    )


def test_targeted_integrated_qa_restores_unambiguous_missing_issue_id() -> None:
    result = parse_translation_response(
        json.dumps(
            {
                "translations": [{"id": 1, "text": "Câu đã được sửa."}],
                "glossary_updates": [],
                "issues": [
                    {
                        "severity": "blocker",
                        "code": "context",
                        "message": "Câu vẫn cần sửa.",
                        "suggestion": "Câu đúng.",
                    }
                ],
            },
            ensure_ascii=False,
        ),
        expected_cues=_cues(1),
    )

    assert result.qa_issues[0].index == 1


def test_integrated_qa_format_error_keeps_complete_translation_and_fails_closed() -> None:
    result = parse_translation_response(
        json.dumps(
            {
                "translations": [
                    {"id": 1, "text": "Câu một."},
                    {"id": 2, "text": "Câu hai."},
                ],
                "glossary_updates": [],
                "issues": [
                    {
                        "severity": "blocker",
                        "code": "context",
                        "message": "Một câu vẫn cần sửa.",
                        "suggestion": "Hãy sửa lại.",
                    }
                ],
            },
            ensure_ascii=False,
        ),
        expected_cues=_cues(2),
    )

    assert [value.text for value in result.values] == ["Câu một.", "Câu hai."]
    assert [issue.index for issue in result.qa_issues] == [1, 2]
    assert {issue.code for issue in result.qa_issues} == {"malformed_integrated_qa"}
    assert all(
        issue.severity is TranslationQaSeverity.BLOCKER for issue in result.qa_issues
    )


def test_local_qa_blocks_cjk_and_known_untranslated_latin_but_only_warns_long_line() -> None:
    issues = local_translation_qa(
        (
            TranslationValue("a", 1, "Daozhang vẫn đang ở 山门."),
            TranslationValue("b", 2, "Đây là " + "một câu rất dài " * 4),
            TranslationValue("c", 3, "Đây là " + "một câu cực kỳ dài " * 10),
        ),
        banned_latin_terms=("Daozhang",),
        long_line_chars=40,
    )

    by_code = {issue.code: issue for issue in issues}
    assert by_code["cjk_remaining"].severity is TranslationQaSeverity.BLOCKER
    assert by_code["untranslated_latin_term"].severity is TranslationQaSeverity.BLOCKER
    assert by_code["long_line"].severity is TranslationQaSeverity.WARNING
    assert by_code["overlong_translation"].severity is TranslationQaSeverity.BLOCKER


def test_integrated_qa_blocks_film_bible_names_and_warns_other_terms() -> None:
    cue = SubtitleCue(
        "cue-name",
        1,
        TimeRange(0, 1_000_000),
        "林可儿服用聚气丹。",
        "zh",
    )
    chunk = plan_chunks(semantic_groups((cue,)))[0]
    bible = FilmBible(
        terms=(
            FilmTerm("林可儿", "Lâm Khả Xuyên", "name"),
            FilmTerm("聚气丹", "Tụ Khí Đan", "term"),
        )
    )

    issues = film_bible_translation_qa(
        chunk,
        (TranslationValue("cue-name", 1, "Lâm Khả Nhi uống Tụ Linh Đan."),),
        film_bible=bible,
    )

    by_code = {issue.code: issue for issue in issues}
    assert by_code["film_bible_name_mismatch"].severity is TranslationQaSeverity.BLOCKER
    assert by_code["film_bible_term_mismatch"].severity is TranslationQaSeverity.WARNING
    assert not film_bible_translation_qa(
        chunk,
        (TranslationValue("cue-name", 1, "Lâm Khả Xuyên uống Tụ Khí Đan."),),
        film_bible=bible,
    )


def test_film_bible_name_match_requires_complete_word_boundary() -> None:
    cue = SubtitleCue(
        "cue-short-name",
        1,
        TimeRange(0, 1_000_000),
        "安已经来了。",
        "zh",
    )
    chunk = plan_chunks(semantic_groups((cue,)))[0]
    bible = FilmBible(terms=(FilmTerm("安", "An", "name"),))

    issues = film_bible_translation_qa(
        chunk,
        (TranslationValue("cue-short-name", 1, "Anh ấy đã đến rồi."),),
        film_bible=bible,
    )

    assert [issue.code for issue in issues] == ["film_bible_name_mismatch"]
    assert not film_bible_translation_qa(
        chunk,
        (TranslationValue("cue-short-name", 1, "An đã đến rồi."),),
        film_bible=bible,
    )


def test_subtitle_wrap_preserves_every_word_and_uses_at_most_two_lines() -> None:
    original = (
        "Đây là một câu thoại tiếng Việt tương đối dài cần được xuống dòng hợp lý "
        "để người xem có thể đọc tự nhiên."
    )
    wrapped = wrap_vietnamese_subtitle(original, max_chars=46)

    assert wrapped.replace("\n", " ") == original
    assert len(wrapped.splitlines()) == 2


def test_subtitle_wrap_default_keeps_up_to_sixty_eight_characters_on_one_line() -> None:
    sixty_eight = "a" * 68
    longer = "Một " + "câu chữ " * 10

    assert wrap_vietnamese_subtitle(sixty_eight) == sixty_eight
    assert "\n" in wrap_vietnamese_subtitle(longer)


def test_qa_parser_rejects_unknown_ids_and_invalid_severity() -> None:
    with pytest.raises(ValueError, match="unexpected cue"):
        parse_qa_response(
            '{"issues":[{"id":3,"severity":"blocker","code":"wrong",'
            '"message":"Sai","suggestion":"Sửa"}]}',
            allowed_ids=(1, 2),
        )
    with pytest.raises(ValueError, match="invalid severity"):
        parse_qa_response(
            '{"issues":[{"id":1,"severity":"fatal","code":"wrong",'
            '"message":"Sai","suggestion":"Sửa"}]}',
            allowed_ids=(1,),
        )


def test_qa_parser_normalizes_common_gemini_severity_variants() -> None:
    issues = parse_qa_response(
        '{"issues":['
        '{"id":1,"severity":" BLOCKER ","code":"cjk","message":"Sai"},'
        '{"id":2,"severity":"warn","code":"style","message":"Nên sửa"}'
        ']}',
        allowed_ids=(1, 2),
    )

    assert issues[0].severity is TranslationQaSeverity.BLOCKER
    assert issues[1].severity is TranslationQaSeverity.WARNING


def test_qa_parser_recovers_model_example_id_from_unique_cue_reference() -> None:
    issues = parse_qa_response(
        '{"issues":[{"id":1,"severity":"blocker",'
        '"code":"NAME_INCONSISTENCY",'
        '"message":"CUE 947 dùng sai tên nhân vật.",'
        '"suggestion":"Sửa cue 947 thành Lâm Khả Nhi."}]}',
        allowed_ids=tuple(range(945, 968)),
    )

    assert len(issues) == 1
    assert issues[0].index == 947


def test_qa_parser_keeps_valid_id_when_message_mentions_context_cue() -> None:
    issues = parse_qa_response(
        '{"issues":[{"id":945,"severity":"warning",'
        '"code":"CONTEXT","message":"Cue 945 cần đối chiếu cue 947.",'
        '"suggestion":"Giữ nguyên nếu ngữ cảnh đúng."}]}',
        allowed_ids=tuple(range(945, 968)),
    )

    assert len(issues) == 1
    assert issues[0].index == 945


def test_qa_parser_never_persists_a_cjk_replacement_suggestion() -> None:
    issues = parse_qa_response(
        json.dumps(
            {
                "issues": [
                    {
                        "id": 1,
                        "severity": "blocker",
                        "code": "NAME_INCONSISTENCY",
                        "message": "Tên riêng chưa đúng.",
                        "suggestion": "Tôn Nh晔 đã đến.",
                    }
                ]
            },
            ensure_ascii=False,
        ),
        allowed_ids=(1,),
    )

    assert issues[0].suggestion == ""


def test_qa_review_reopens_on_resume_without_losing_translation_cache() -> None:
    cues = _cues(2)
    chunks = plan_chunks(semantic_groups(cues))
    ledger = build_ledger(
        cues,
        chunks,
        provider_id="gemini-web",
        model_id="selected-on-web",
    )
    values = (
        TranslationValue("cue-1", 1, "Ta bắt đầu tu luyện."),
        TranslationValue("cue-2", 2, "Sau đó sẽ thành tiên."),
    )
    review = mark_chunk_complete(
        ledger,
        chunk_index=0,
        values=values,
        qa_issues=(
            TranslationQaIssue(
                1,
                "context",
                TranslationQaSeverity.BLOCKER,
                "Bản dịch cần sửa lại theo ngữ cảnh.",
            ),
        ),
        film_bible=ledger.film_bible,
        attempts=3,
    )

    reopened = reopen_qa_reviews_for_resume(review)

    assert reopened.chunks[0].state is ChunkState.PENDING
    assert reopened.chunks[0].qa_issues == review.chunks[0].qa_issues
    assert reopened.translations == review.translations


def test_resume_replans_only_unfinished_suffix_and_keeps_every_cached_translation() -> None:
    cues = _numbered_cues(320)
    old_chunks = plan_chunks(
        semantic_groups(cues),
        max_cues=24,
        max_source_chars=1_800,
    )
    ledger = build_ledger(
        cues,
        old_chunks,
        provider_id="gemini-web",
        model_id="selected-on-web",
    )
    for chunk in old_chunks[:2]:
        ledger = mark_chunk_complete(
            ledger,
            chunk_index=chunk.index,
            values=tuple(
                TranslationValue(cue.id, cue.index, f"Bản dịch {cue.index}.") for cue in chunk.cues
            ),
            qa_issues=(),
            film_bible=ledger.film_bible,
            attempts=1,
        )
    review_chunk = old_chunks[2]
    ledger = mark_chunk_complete(
        ledger,
        chunk_index=review_chunk.index,
        values=tuple(
            TranslationValue(cue.id, cue.index, f"Bản dịch {cue.index}.")
            for cue in review_chunk.cues
        ),
        qa_issues=(
            TranslationQaIssue(
                review_chunk.ids[0],
                "context",
                TranslationQaSeverity.BLOCKER,
                "Cần QA lại theo ngữ cảnh.",
            ),
        ),
        film_bible=ledger.film_bible,
        attempts=2,
    )
    cached = dict(ledger.translations)
    cached.update({index: f"Bản dịch {index}." for index in range(73, 83)})
    ledger = replace(ledger, translations=cached)

    replanned, chunks = replan_ledger_for_resume(
        ledger,
        cues,
        max_cues=150,
        max_source_chars=8_000,
    )

    assert [chunk.ids for chunk in chunks] == [chunk.ids for chunk in old_chunks]
    assert [item.state for item in replanned.chunks[:2]] == [
        ChunkState.QA_PASS,
        ChunkState.QA_PASS,
    ]
    assert replanned.chunks[2].state is ChunkState.PENDING
    assert all(item.state is ChunkState.PENDING for item in replanned.chunks[3:])
    assert dict(replanned.translations) == cached
    assert sorted(replanned.translations) == list(range(1, 83))


def test_resume_reopens_only_review_chunks_and_preserves_later_qa_pass_chunks() -> None:
    cues = _numbered_cues(6)
    chunks = plan_chunks(
        semantic_groups(cues, max_group_cues=2),
        max_cues=2,
        max_source_chars=1_800,
    )
    ledger = build_ledger(
        cues,
        chunks,
        provider_id="gemini-web",
        model_id="selected-on-web",
    )
    for chunk in chunks:
        issues = (
            TranslationQaIssue(
                chunk.ids[0],
                "cjk_remaining",
                TranslationQaSeverity.BLOCKER,
                "Còn chữ Hán.",
            ),
        ) if chunk.index == 1 else ()
        ledger = mark_chunk_complete(
            ledger,
            chunk_index=chunk.index,
            values=tuple(
                TranslationValue(cue.id, cue.index, f"Bản dịch {cue.index}.")
                for cue in chunk.cues
            ),
            qa_issues=issues,
            film_bible=ledger.film_bible,
            attempts=1,
        )

    replanned, resumed_chunks = replan_ledger_for_resume(
        ledger,
        cues,
        max_cues=150,
        max_source_chars=8_000,
    )

    assert [item.state for item in replanned.chunks] == [
        ChunkState.QA_PASS,
        ChunkState.PENDING,
        ChunkState.QA_PASS,
    ]
    assert [chunk.ids for chunk in resumed_chunks] == [chunk.ids for chunk in chunks]


def test_ledger_round_trip_preserves_cache_and_review_state() -> None:
    cues = _cues(2)
    chunks = plan_chunks(semantic_groups(cues))
    ledger = build_ledger(
        cues,
        chunks,
        provider_id="gemini-api",
        model_id="gemini-flash",
        film_bible=FilmBible(profile="xianxia"),
    )
    values = (
        TranslationValue("cue-1", 1, "Ta bắt đầu tu luyện."),
        TranslationValue("cue-2", 2, "Sau đó sẽ thành tiên."),
    )
    qa = parse_qa_response(
        json.dumps(
            {
                "issues": [
                    {
                        "id": 2,
                        "severity": "blocker",
                        "code": "context",
                        "message": "Ngữ cảnh cần kiểm tra.",
                        "suggestion": "Đối chiếu câu trước.",
                    }
                ]
            },
            ensure_ascii=False,
        ),
        allowed_ids=(1, 2),
    )
    updated = mark_chunk_complete(
        ledger,
        chunk_index=0,
        values=values,
        qa_issues=qa,
        film_bible=ledger.film_bible,
        attempts=2,
    )

    restored = ledger_from_dict(ledger_to_dict(updated))

    assert restored.chunks[0].state is ChunkState.QA_REVIEW
    assert restored.translations[2] == "Sau đó sẽ thành tiên."
    assert restored.chunks[0].attempts == 2


def test_legacy_ledger_drops_poisoned_film_bible_without_losing_cached_cues() -> None:
    cues = _cues(2)
    chunks = plan_chunks(semantic_groups(cues))
    ledger = build_ledger(
        cues,
        chunks,
        provider_id="gemini-web",
        model_id="selected-on-web",
    )
    raw = ledger_to_dict(
        replace(
            ledger,
            translations={1: "Đã biết chưởng quỹ đang盯着 mình.", 2: "Câu sạch."},
        )
    )
    raw["film_bible"] = {
        "title": "Phim",
        "profile": "xianxia",
        "synopsis": "Chuyện của 苏雨萱.",
        "characters": ["Diệp Thần", "Tô Vũ萱"],
        "terms": [
            {"source": "叶晨", "vi": "Diệp Thần", "kind": "name"},
            {"source": "苏雨萱", "vi": "Tô Vũ萱", "kind": "name"},
        ],
    }

    restored = ledger_from_dict(raw)

    assert dict(restored.translations) == {
        1: "Đã biết chưởng quỹ đang盯着 mình.",
        2: "Câu sạch.",
    }
    assert restored.translations[1] == "Đã biết chưởng quỹ đang盯着 mình."
    assert restored.film_bible.synopsis == ""
    assert restored.film_bible.characters == ("Diệp Thần",)
    assert restored.film_bible.terms == (FilmTerm("叶晨", "Diệp Thần", "name"),)


def test_4333_cue_plan_has_exact_coverage_and_stable_ledger_roundtrip() -> None:
    cues = tuple(
        SubtitleCue(
            f"cue-{index}",
            index,
            TimeRange((index - 1) * 700_000, index * 700_000),
            f"第{index}句话。",
            "zh",
        )
        for index in range(1, 4_334)
    )
    chunks = plan_chunks(semantic_groups(cues), max_cues=24)
    ledger = build_ledger(cues, chunks, provider_id="fixture", model_id="deterministic")

    assert [index for chunk in chunks for index in chunk.ids] == list(range(1, 4_334))
    assert ledger_from_dict(ledger_to_dict(ledger)).signature == ledger.signature
