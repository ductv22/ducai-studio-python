from __future__ import annotations

import json
import re
import unicodedata
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from enum import StrEnum
from types import MappingProxyType

from ducai_studio.domain.fingerprint import stable_fingerprint
from ducai_studio.domain.jobs import JsonValue
from ducai_studio.domain.project import SubtitleCue
from ducai_studio.domain.subtitles import VIETNAMESE_SUBTITLE_WRAP_CHARS
from ducai_studio.providers.translation.contracts import TranslationPurpose, TranslationRequest

_CJK = re.compile(
    r"[\u3400-\u9fff\uf900-\ufaff"
    r"\U00020000-\U0002a6df\U0002a700-\U0002ebef"
    r"\U0002f800-\U0002fa1f\U00030000-\U000323af]"
)
_TRAILING_COMMA = re.compile(r",\s*([}\]])")
_FENCE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.IGNORECASE)
_SENTENCE_END = re.compile(r"[。！？!?…][\"'”’）】》]?$|\.{2,}$")
_VIETNAMESE_MARK = re.compile(
    r"[ăâđêôơưáàảãạấầẩẫậắằẳẵặéèẻẽẹếềểễệíìỉĩị"
    r"óòỏõọốồổỗộớờởỡợúùủũụứừửữựýỳỷỹỵ]",
    re.IGNORECASE,
)
_TRANSLATION_STYLE_PRESETS = frozenset(
    {"legacy", "auto", "xianxia", "wuxia", "palace", "modern", "neutral"}
)
_ANCIENT_STYLE_PRESETS = frozenset({"xianxia", "wuxia", "palace"})
STYLE_QA_CODES = frozenset(
    {"ancient_modern_pronoun", "ancient_title_order", "ancient_alias_mismatch"}
)
_CONSISTENCY_RULE = (
    "NHẤT QUÁN: tên, địa danh và thuật ngữ trong FILM_BIBLE đã được chốt; "
    "dùng đúng cách gọi đã có, không tự đổi cách viết hoặc loại mục. "
    "glossary_updates chỉ bổ sung mục mới, không lặp lại hay thay thế mục đã chốt. "
    "Giữ xưng hô theo từng cặp nhân vật có bằng chứng trong nguồn và ngữ cảnh; "
    "không tự đoán danh tính người nói từ một đại từ đơn lẻ hoặc ép cùng xưng hô "
    "cho những người khác nhau."
)


def contains_cjk(value: str) -> bool:
    """Return whether a value still contains a Han/CJK codepoint."""

    return _CJK.search(value) is not None


def _contains_normalized_phrase(text: str, phrase: str) -> bool:
    """Match a complete normalized Vietnamese/Latin phrase, not a substring."""

    normalized_text = " ".join(unicodedata.normalize("NFC", text).split()).casefold()
    normalized_phrase = " ".join(unicodedata.normalize("NFC", phrase).split()).casefold()
    if not normalized_phrase:
        return False
    return (
        re.search(
            rf"(?<!\w){re.escape(normalized_phrase)}(?!\w)",
            normalized_text,
        )
        is not None
    )


class TranslationQaSeverity(StrEnum):
    WARNING = "warning"
    BLOCKER = "blocker"


@dataclass(frozen=True, slots=True)
class FilmTerm:
    source: str
    vietnamese: str
    kind: str = "term"

    def __post_init__(self) -> None:
        if not self.source.strip() or not self.vietnamese.strip() or not self.kind.strip():
            raise ValueError("Film term source, Vietnamese text and kind are required")
        if contains_cjk(self.vietnamese):
            raise ValueError("Film term Vietnamese text must not contain CJK")


@dataclass(frozen=True, slots=True)
class FilmBible:
    title: str = ""
    profile: str = "neutral"
    synopsis: str = ""
    terms: tuple[FilmTerm, ...] = ()
    characters: tuple[str, ...] = ()
    # Appended for positional compatibility with ledgers created before a105.
    # ``legacy`` deliberately omits the new policy from prompts/fingerprints.
    style_preset: str = "legacy"
    custom_rules: str = ""

    def __post_init__(self) -> None:
        if not self.profile.strip():
            raise ValueError("Film Bible profile is required")
        if self.style_preset not in _TRANSLATION_STYLE_PRESETS:
            raise ValueError("Film Bible translation style is invalid")
        if len(self.custom_rules) > 4_000:
            raise ValueError("Film Bible custom translation rules are too long")
        if contains_cjk(self.synopsis) or any(contains_cjk(value) for value in self.characters):
            raise ValueError("Film Bible Vietnamese metadata must not contain CJK")
        sources = [term.source.casefold() for term in self.terms]
        if len(sources) != len(set(sources)):
            raise ValueError("Film Bible term sources must be unique")

    def merge_terms(self, values: Iterable[FilmTerm]) -> FilmBible:
        merged = {term.source.casefold(): term for term in self.terms}
        for item in values:
            # Repeated entries must not downgrade a locked name/place to a
            # generic term, which would weaken the deterministic identity gate.
            merged.setdefault(item.source.casefold(), item)
        return replace(self, terms=tuple(merged[key] for key in sorted(merged)))

    @property
    def resolved_style(self) -> str:
        """Resolve auto only after the Film Bible has classified the source."""

        if self.style_preset == "auto":
            profile = self.profile.strip().casefold()
            return profile if profile in _ANCIENT_STYLE_PRESETS | {"modern"} else "neutral"
        return self.style_preset


@dataclass(frozen=True, slots=True)
class SemanticGroup:
    id: str
    cues: tuple[SubtitleCue, ...]

    def __post_init__(self) -> None:
        if not self.id.strip() or not self.cues:
            raise ValueError("Semantic group id and cues are required")


@dataclass(frozen=True, slots=True)
class TranslationChunk:
    index: int
    groups: tuple[SemanticGroup, ...]

    def __post_init__(self) -> None:
        if self.index < 0 or not self.groups:
            raise ValueError("Translation chunk index and groups are required")

    @property
    def cues(self) -> tuple[SubtitleCue, ...]:
        return tuple(cue for group in self.groups for cue in group.cues)

    @property
    def ids(self) -> tuple[int, ...]:
        return tuple(cue.index for cue in self.cues)


@dataclass(frozen=True, slots=True)
class TranslationValue:
    cue_id: str
    index: int
    text: str

    def __post_init__(self) -> None:
        if not self.cue_id.strip() or self.index <= 0 or not self.text.strip():
            raise ValueError("Translated cue id, index and text are required")


@dataclass(frozen=True, slots=True)
class TranslationQaIssue:
    index: int
    code: str
    severity: TranslationQaSeverity
    message: str
    suggestion: str = ""
    # Preserve the severity returned by the provider even when the product
    # treats semantic findings as non-blocking warnings.
    original_severity: TranslationQaSeverity | None = None

    def __post_init__(self) -> None:
        if self.index <= 0 or not self.code.strip() or not self.message.strip():
            raise ValueError("QA issue index, code and message are required")
        if self.original_severity is None:
            object.__setattr__(self, "original_severity", self.severity)
        else:
            object.__setattr__(
                self, "original_severity", TranslationQaSeverity(self.original_severity)
            )


_NON_BLOCKING_QA_CODES = frozenset(
    {
        "film_bible_name_mismatch",
        "film_bible_term_mismatch",
        "cjk_in_translation",
        "cjk_characters_present",
        "latin_characters_present",
        "banned_latin_term",
        "translation_error",
        "name_inconsistency",
        "CUE_TOO_LONG",
        "cue_too_long",
    }
)


def normalize_semantic_qa_issues(
    issues: Sequence[TranslationQaIssue],
    *,
    keep_cjk_blocker: bool = False,
    only_cjk_blocker: bool = False,
) -> tuple[TranslationQaIssue, ...]:
    """Downgrade semantic/content findings while retaining original evidence."""

    normalized: list[TranslationQaIssue] = []
    for issue in issues:
        code = issue.code.strip()
        non_blocking = (
            code in _NON_BLOCKING_QA_CODES
            or "name" in code.casefold()
            or "cjk" in code.casefold()
            or "latin" in code.casefold()
            or "too_long" in code.casefold()
            or code.casefold().startswith("translation_")
        )
        if only_cjk_blocker:
            normalized_code = code.casefold()
            non_blocking = not (
                "cjk" in normalized_code
                or normalized_code in {"malformed_integrated_qa", "remote_qa_format_unavailable"}
                or normalized_code in {"overlong_translation", "cue_too_long"}
            )
        elif keep_cjk_blocker and "cjk" in code.casefold():
            non_blocking = False
        if non_blocking and issue.severity is TranslationQaSeverity.BLOCKER:
            normalized.append(
                replace(
                    issue,
                    severity=TranslationQaSeverity.WARNING,
                    original_severity=TranslationQaSeverity.BLOCKER,
                )
            )
        else:
            normalized.append(issue)
    return tuple(normalized)


@dataclass(frozen=True, slots=True)
class TranslationChunkResult:
    values: tuple[TranslationValue, ...]
    glossary_updates: tuple[FilmTerm, ...] = ()
    qa_issues: tuple[TranslationQaIssue, ...] = ()


class ChunkState(StrEnum):
    PENDING = "pending"
    TRANSLATED = "translated"
    QA_PASS = "qa_pass"
    QA_REVIEW = "qa_review"


@dataclass(frozen=True, slots=True)
class ChunkLedgerEntry:
    chunk_index: int
    cue_ids: tuple[int, ...]
    state: ChunkState = ChunkState.PENDING
    attempts: int = 0
    response_fingerprint: str = ""
    qa_issues: tuple[TranslationQaIssue, ...] = ()

    def __post_init__(self) -> None:
        if self.chunk_index < 0 or not self.cue_ids or self.attempts < 0:
            raise ValueError("Chunk ledger identity and attempt count are invalid")


@dataclass(frozen=True, slots=True)
class TranslationLedger:
    signature: str
    provider_id: str
    model_id: str
    chunks: tuple[ChunkLedgerEntry, ...]
    translations: Mapping[int, str] = field(default_factory=dict)
    film_bible: FilmBible = field(default_factory=FilmBible)

    def __post_init__(self) -> None:
        if not self.signature.strip() or not self.provider_id.strip() or not self.model_id.strip():
            raise ValueError("Translation ledger identity is required")
        if [item.chunk_index for item in self.chunks] != list(range(len(self.chunks))):
            raise ValueError("Translation ledger chunks must be contiguous")
        values = {int(key): str(value) for key, value in self.translations.items()}
        if any(key <= 0 or not value.strip() for key, value in values.items()):
            raise ValueError("Translation ledger contains an invalid translated cue")
        object.__setattr__(self, "translations", MappingProxyType(values))

    @property
    def complete(self) -> bool:
        return bool(self.chunks) and all(
            chunk.state in {ChunkState.QA_PASS, ChunkState.QA_REVIEW} for chunk in self.chunks
        )


def semantic_groups(
    cues: Sequence[SubtitleCue],
    *,
    max_group_cues: int = 10,
    max_group_chars: int = 280,
    gap_us: int = 2_000_000,
) -> tuple[SemanticGroup, ...]:
    if max_group_cues <= 0 or max_group_chars <= 0 or gap_us < 0:
        raise ValueError("Semantic grouping limits are invalid")
    groups: list[SemanticGroup] = []
    current: list[SubtitleCue] = []
    current_chars = 0
    previous: SubtitleCue | None = None
    for cue in cues:
        split_before = bool(
            current
            and (
                len(current) >= max_group_cues
                or current_chars + len(cue.text) > max_group_chars
                or (previous is not None and cue.range.start_us - previous.range.end_us > gap_us)
            )
        )
        if split_before:
            groups.append(SemanticGroup(f"group-{len(groups) + 1}", tuple(current)))
            current = []
            current_chars = 0
        current.append(cue)
        current_chars += len(cue.text)
        previous = cue
        if _SENTENCE_END.search(cue.text.strip()):
            groups.append(SemanticGroup(f"group-{len(groups) + 1}", tuple(current)))
            current = []
            current_chars = 0
    if current:
        groups.append(SemanticGroup(f"group-{len(groups) + 1}", tuple(current)))
    return tuple(groups)


def plan_chunks(
    groups: Sequence[SemanticGroup],
    *,
    max_cues: int = 24,
    max_source_chars: int = 1_800,
) -> tuple[TranslationChunk, ...]:
    if max_cues <= 0 or max_source_chars <= 0:
        raise ValueError("Translation chunk limits are invalid")
    chunks: list[TranslationChunk] = []
    current: list[SemanticGroup] = []
    cue_count = 0
    char_count = 0
    for group in groups:
        group_cues = len(group.cues)
        group_chars = sum(len(cue.text) for cue in group.cues)
        if current and (
            cue_count + group_cues > max_cues or char_count + group_chars > max_source_chars
        ):
            chunks.append(TranslationChunk(len(chunks), tuple(current)))
            current = []
            cue_count = 0
            char_count = 0
        current.append(group)
        cue_count += group_cues
        char_count += group_chars
    if current:
        chunks.append(TranslationChunk(len(chunks), tuple(current)))
    return tuple(chunks)


def build_ledger(
    cues: Sequence[SubtitleCue],
    chunks: Sequence[TranslationChunk],
    *,
    provider_id: str,
    model_id: str,
    film_bible: FilmBible | None = None,
) -> TranslationLedger:
    signature_payload: dict[str, object] = {
        "cues": [
            {
                "id": cue.id,
                "index": cue.index,
                "start_us": cue.range.start_us,
                "end_us": cue.range.end_us,
                "text": cue.text,
            }
            for cue in cues
        ],
        "provider": provider_id,
        "model": model_id,
    }
    if film_bible is not None and film_bible.style_preset != "legacy":
        signature_payload["translation_style"] = {
            "preset": film_bible.style_preset,
            "custom_rules": film_bible.custom_rules,
        }
    signature = stable_fingerprint(signature_payload)
    return TranslationLedger(
        signature,
        provider_id,
        model_id,
        tuple(ChunkLedgerEntry(chunk.index, chunk.ids) for chunk in chunks),
        film_bible=film_bible or FilmBible(),
    )


def _film_bible_payload(film_bible: FilmBible) -> dict[str, JsonValue]:
    payload: dict[str, JsonValue] = {
        "title": film_bible.title,
        "profile": film_bible.profile,
        "synopsis": film_bible.synopsis,
        "characters": list(film_bible.characters),
        "terms": [
            {"source": item.source, "vi": item.vietnamese, "kind": item.kind}
            for item in film_bible.terms
        ],
    }
    if film_bible.style_preset != "legacy":
        payload["translation_style"] = {
            "preset": film_bible.style_preset,
            "resolved": film_bible.resolved_style,
            "custom_rules": film_bible.custom_rules,
        }
    return payload


def _style_prompt(film_bible: FilmBible) -> str:
    if film_bible.style_preset == "legacy":
        return ""
    labels = {
        "auto": "Tự nhận diện theo Film Bible",
        "xianxia": "Tu tiên cổ trang",
        "wuxia": "Võ hiệp",
        "palace": "Cung đình/cổ trang",
        "modern": "Hiện đại",
        "neutral": "Trung tính",
    }
    lines = [
        "VĂN PHONG DỰ ÁN:",
        f"- Preset: {labels[film_bible.style_preset]}; phong cách áp dụng: "
        f"{film_bible.resolved_style}.",
        "- Giữ nhất quán xưng hô, danh xưng, thuật ngữ và quan hệ xuyên suốt phim; "
        "chỉ suy luận vai vế khi có bằng chứng trong nguồn/FILM_BIBLE.",
        "- Viết tiếng Việt tự nhiên, dễ đọc; không bê nguyên trật tự từ tiếng Trung "
        "và không lạm dụng Hán Việt. Giữ tên riêng, vai vế và thuật ngữ thể loại cần thiết, "
        "còn câu thoại thông thường dùng cách diễn đạt quen thuộc.",
        "- Ví dụ trật tự nghĩa: 'thần tiên sống', không phải 'sống thần tiên'. "
        "Với dược liệu, chỉ dùng 'quý' nếu nguồn/ngữ cảnh nói về giá trị; nếu nói "
        "kích thước thì vẫn dùng 'lớn'. Không thay từ máy móc hoặc thêm ý nguồn không có.",
    ]
    if film_bible.resolved_style in _ANCIENT_STYLE_PRESETS:
        lines.extend(
            (
                "- Tránh mày/tao, anh/em hiện đại khi ngữ cảnh không cho phép; ưu tiên "
                "ta/ngươi, tại hạ, đạo hữu, tiền bối, bổn tọa, vi sư, đệ tử, sư huynh, "
                "sư tỷ đúng vai vế.",
                "- Tên riêng đứng trước hậu tố thân mật khi phù hợp: Hồng Ly tỷ tỷ, "
                "không viết tỷ tỷ Hồng Ly.",
                "- Với tên gọi/biệt danh trong nguồn: 阿狗 → A Cẩu, 阿猫 → A Miêu, "
                "老狗 → Lão Cẩu. Không đổi từ chó/mèo chỉ động vật, câu chửi hoặc văn "
                "cảnh hiện đại thành cẩu/miêu một cách máy móc.",
            )
        )
    if film_bible.custom_rules.strip():
        lines.append("- Quy tắc riêng của người dùng (ưu tiên nếu không làm sai nghĩa):")
        lines.append(film_bible.custom_rules.strip())
    return "\n".join(lines)


def _film_term_schema() -> dict[str, JsonValue]:
    return {
        "type": "object",
        "properties": {
            "source": {"type": "string"},
            "vi": {"type": "string"},
            "kind": {"type": "string", "enum": ["name", "term", "place"]},
        },
        "required": ["source", "vi", "kind"],
    }


def _translation_response_schema() -> dict[str, JsonValue]:
    return {
        "type": "object",
        "properties": {
            "translations": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "integer"},
                        "text": {"type": "string"},
                    },
                    "required": ["id", "text"],
                },
            },
            "glossary_updates": {
                "type": "array",
                "items": _film_term_schema(),
            },
            "issues": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "integer"},
                        "severity": {
                            "type": "string",
                            "enum": ["warning", "blocker"],
                        },
                        "code": {"type": "string"},
                        "message": {"type": "string"},
                        "suggestion": {"type": "string"},
                    },
                    "required": [
                        "id",
                        "severity",
                        "code",
                        "message",
                        "suggestion",
                    ],
                },
            },
        },
        "required": ["translations", "glossary_updates", "issues"],
    }


def _film_bible_response_schema() -> dict[str, JsonValue]:
    return {
        "type": "object",
        "properties": {
            "profile": {
                "type": "string",
                "enum": ["xianxia", "wuxia", "palace", "modern", "neutral"],
            },
            "synopsis": {"type": "string"},
            "characters": {"type": "array", "items": {"type": "string"}},
            "terms": {"type": "array", "items": _film_term_schema()},
        },
        "required": ["profile", "synopsis", "characters", "terms"],
    }


def _qa_response_schema() -> dict[str, JsonValue]:
    return {
        "type": "object",
        "properties": {
            "issues": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "integer"},
                        "severity": {
                            "type": "string",
                            "enum": ["warning", "blocker"],
                        },
                        "code": {"type": "string"},
                        "message": {"type": "string"},
                        "suggestion": {"type": "string"},
                    },
                    "required": ["id", "severity", "code", "message", "suggestion"],
                },
            }
        },
        "required": ["issues"],
    }


def translation_request(
    chunk: TranslationChunk,
    *,
    film_bible: FilmBible,
    previous: Sequence[TranslationValue] = (),
    following: Sequence[SubtitleCue] = (),
    compact_recovery: bool = False,
) -> TranslationRequest:
    rows = [
        {
            "id": cue.index,
            "group": group.id,
            "zh": cue.text,
            "start_us": cue.range.start_us,
            "end_us": cue.range.end_us,
            "slot_duration_ms": round(cue.range.duration_us / 1_000),
            "role": cue.role,
            "orientation": cue.orientation,
            "style_hint": cue.style_hint,
            "soft_max_chars": max(
                6,
                round(cue.range.duration_us / 1_000_000 * 18),
            ),
        }
        for group in chunk.groups
        for cue in group.cues
    ]
    context = {
        "previous": [{"id": value.index, "vi": value.text} for value in previous[-12:]],
        "following": [{"id": cue.index, "zh": cue.text} for cue in following[:6]],
    }
    rules = (
        "Dịch tự nhiên, mạch lạc sang tiếng Việt; giữ đúng nội dung đang diễn ra; "
        "không thêm tình tiết; thống nhất tên riêng, tông môn, địa danh, yêu thú và thuật ngữ; "
        "không để lại chữ Hán hay pinyin/Latin chưa Việt hóa; "
        "mỗi ID phải có đúng một câu không rỗng. "
        "Có thể diễn đạt thoát ý để dễ hiểu nhưng không đổi nghĩa. "
        "Ưu tiên câu thoại gọn, tự nhiên và phù hợp với thời lượng cue; bỏ lặp từ, "
        "không thêm lời dẫn giải làm câu dài hơn cần thiết. Mỗi CUES có "
        "slot_duration_ms và soft_max_chars: coi đây là ngân sách mềm để chọn cách "
        "diễn đạt súc tích, tuyệt đối không cắt mất nghĩa, phủ định, tên riêng hay ý chính. "
        "Trước khi trả JSON, tự kiểm tra ngay trong cùng lượt từng câu với nguồn, "
        "CONTEXT và FILM_BIBLE; tự sửa mọi lỗi sai nghĩa, mất ý, tên không nhất quán, "
        "câu cụt/lặp và ký tự CJK. Sau khi đã tự sửa, issues chỉ ghi blocker thật sự "
        "còn chưa thể tự giải quyết; nếu không còn blocker thì trả mảng rỗng. "
        "Cue role=scene_text là chữ giới thiệu nhân vật, chiêu thức, pháp bảo hoặc địa danh "
        "được đặt lại trực tiếp trong cảnh: dịch như nhãn ngắn gọn, không biến thành câu thoại, "
        "giữ đúng danh xưng và ưu tiên độ gọn để khớp panel; orientation/style_hint chỉ mô tả "
        "cách trình bày, không được thêm nội dung."
    )
    if compact_recovery:
        rules += " Đây là lượt cứu lỗi: chỉ trả JSON ngắn, không giải thích và đủ đúng mọi ID."
    prompt = "\n".join(
        part
        for part in (
            rules,
            _CONSISTENCY_RULE,
            _style_prompt(film_bible),
            "Không xóa, gộp, tách hoặc đổi ID. Xuống dòng bên trong text khi câu dài.",
            'Trả đúng JSON: {"translations":[{"id":1,"text":"..."}],'
            '"glossary_updates":[{"source":"...","vi":"...","kind":"name|term|place"}],'
            '"issues":[]}. Mỗi issue nếu có phải gồm id, severity="blocker", code, '
            'message và suggestion là câu Việt thay thế hoàn chỉnh hoặc "".',
            "FILM_BIBLE=" + json.dumps(_film_bible_payload(film_bible), ensure_ascii=False),
            "CONTEXT=" + json.dumps(context, ensure_ascii=False),
            "CUES=" + json.dumps(rows, ensure_ascii=False),
        )
        if part
    )
    return TranslationRequest(
        request_id=f"translate-{chunk.index}",
        purpose=TranslationPurpose.TRANSLATE,
        prompt=prompt,
        expected_ids=chunk.ids,
        temperature=0.2,
        response_schema=_translation_response_schema(),
    )


def film_bible_request(
    cues: Sequence[SubtitleCue],
    *,
    title: str = "",
    sample_size: int = 180,
    style_preset: str = "legacy",
    custom_rules: str = "",
) -> TranslationRequest:
    if sample_size <= 0:
        raise ValueError("Film Bible sample size must be positive")
    if len(cues) <= sample_size:
        sample = tuple(cues)
    else:
        third = max(1, sample_size // 3)
        middle = max(0, len(cues) // 2 - third // 2)
        sample = tuple(cues[:third]) + tuple(cues[middle : middle + third]) + tuple(cues[-third:])
    if style_preset not in _TRANSLATION_STYLE_PRESETS:
        raise ValueError("Film Bible translation style is invalid")
    if len(custom_rules) > 4_000:
        raise ValueError("Film Bible custom translation rules are too long")
    rows = [{"id": cue.index, "zh": cue.text} for cue in sample]
    style_context = FilmBible(
        title=title,
        style_preset=style_preset,
        custom_rules=custom_rules,
    )
    prompt = "\n".join(
        part
        for part in (
            "Phân tích mẫu thoại để tạo Film Bible cho dịch phim Trung sang tiếng Việt. "
            "Không bịa cốt truyện ngoài dữ liệu. Chỉ ghi nhân vật/thuật ngữ có bằng chứng.",
            _style_prompt(style_context),
            "Nếu preset là Tự nhận diện, profile phải phản ánh đúng thể loại để các bước "
            "dịch sau áp dụng xưng hô và danh xưng phù hợp.",
            'Trả đúng JSON: {"profile":"xianxia|wuxia|palace|modern|neutral",'
            '"synopsis":"...","characters":["..."],'
            '"terms":[{"source":"...","vi":"...","kind":"name|term|place"}]}',
            "TITLE=" + title,
            "CUES=" + json.dumps(rows, ensure_ascii=False),
        )
        if part
    )
    return TranslationRequest(
        request_id="film-bible",
        purpose=TranslationPurpose.FILM_BIBLE,
        prompt=prompt,
        expected_ids=(),
        temperature=0.0,
        response_schema=_film_bible_response_schema(),
    )


def parse_film_bible_response(
    raw: str,
    *,
    title: str = "",
    style_preset: str = "legacy",
    custom_rules: str = "",
) -> FilmBible:
    payload = parse_json_object(raw)
    profile = payload.get("profile", "neutral")
    synopsis = payload.get("synopsis", "")
    characters = payload.get("characters", [])
    terms = payload.get("terms", [])
    if not isinstance(profile, str) or not profile.strip():
        raise ValueError("Film Bible profile is invalid")
    if not isinstance(synopsis, str):
        raise ValueError("Film Bible synopsis is invalid")
    if not isinstance(characters, list) or not all(
        isinstance(value, str) and value.strip() for value in characters
    ):
        raise ValueError("Film Bible characters are invalid")
    if not isinstance(terms, list):
        raise ValueError("Film Bible terms are invalid")
    parsed_terms: list[FilmTerm] = []
    for item in terms:
        if not isinstance(item, dict):
            raise ValueError("Film Bible term is invalid")
        source = item.get("source")
        vietnamese = item.get("vi")
        kind = item.get("kind", "term")
        if not all(isinstance(value, str) for value in (source, vietnamese, kind)):
            raise ValueError("Film Bible term fields are invalid")
        if contains_cjk(vietnamese):
            continue
        parsed_terms.append(FilmTerm(source, vietnamese, kind))
    return FilmBible(
        title=title,
        profile=profile,
        synopsis="" if contains_cjk(synopsis) else synopsis,
        terms=tuple(parsed_terms),
        characters=tuple(value for value in characters if not contains_cjk(value)),
        style_preset=style_preset,
        custom_rules=custom_rules,
    )


def repair_request(
    chunk: TranslationChunk,
    values: Sequence[TranslationValue],
    issues: Sequence[TranslationQaIssue],
    *,
    film_bible: FilmBible,
) -> TranslationRequest:
    source_by_index = {cue.index: cue for cue in chunk.cues}
    issue_by_index: dict[int, list[dict[str, str]]] = {}
    for issue in issues:
        issue_by_index.setdefault(issue.index, []).append(
            {
                "code": issue.code,
                "message": issue.message,
                "suggestion": issue.suggestion,
            }
        )
    rows = [
        {
            "id": value.index,
            "zh": source_by_index[value.index].text,
            "vi": value.text,
            "role": source_by_index[value.index].role,
            "orientation": source_by_index[value.index].orientation,
            "style_hint": source_by_index[value.index].style_hint,
            "issues": issue_by_index.get(value.index, []),
        }
        for value in values
    ]
    has_timing_repair = any(issue.code == "CUE_TOO_LONG" for issue in issues)
    timing_rule = (
        " Với lỗi CUE_TOO_LONG, phải rút gọn mạnh theo giới hạn ký tự ghi trong issue; "
        "giữ nguyên tên riêng, con số, phủ định và toàn bộ ý chính; bỏ từ đệm/lặp và diễn đạt "
        "ngắn tự nhiên, không được chỉ xóa bừa nửa sau câu."
        if has_timing_repair
        else ""
    )
    prompt = "\n".join(
        part
        for part in (
            "Sửa đúng các lỗi QA trong bản dịch. Không thêm tình tiết, không xóa/gộp/tách/đổi ID. "
            "Giữ câu đã đúng; trả đủ mọi ID kể cả cue không cần sửa."
            + timing_rule,
            _CONSISTENCY_RULE,
            _style_prompt(film_bible),
            "Tự kiểm tra lại ngay trong cùng lượt sau khi sửa. Không chạy một vòng QA riêng.",
            'Trả đúng JSON: {"translations":[{"id":1,"text":"..."}],'
            '"glossary_updates":[{"source":"...","vi":"...","kind":"name|term|place"}],'
            '"issues":[]}. Chỉ giữ blocker thật sự chưa giải quyết được trong issues.',
            "FILM_BIBLE=" + json.dumps(_film_bible_payload(film_bible), ensure_ascii=False),
            "CUES=" + json.dumps(rows, ensure_ascii=False),
        )
        if part
    )
    return TranslationRequest(
        request_id=f"repair-{chunk.index}",
        purpose=TranslationPurpose.TRANSLATE,
        prompt=prompt,
        expected_ids=chunk.ids,
        temperature=0.1,
        response_schema=_translation_response_schema(),
    )


def qa_request(
    chunk: TranslationChunk,
    values: Sequence[TranslationValue],
    *,
    film_bible: FilmBible,
) -> TranslationRequest:
    source_by_index = {cue.index: cue for cue in chunk.cues}
    rows = [
        {
            "id": value.index,
            "zh": source_by_index[value.index].text,
            "vi": value.text,
            "role": source_by_index[value.index].role,
            "orientation": source_by_index[value.index].orientation,
            "style_hint": source_by_index[value.index].style_hint,
        }
        for value in values
    ]
    prompt = "\n".join(
        part
        for part in (
            "Bạn là QA độc lập. Chỉ đánh dấu lỗi thật: sai nghĩa/ngữ cảnh, tự thêm tình tiết, "
            "mất ý, tên/thuật ngữ không nhất quán, còn chữ Hán hoặc câu Việt vô nghĩa. "
            "Không viết lại nếu câu đã tự nhiên và đúng.",
            'Trả đúng một object JSON có khóa "issues". Mỗi issue phải có đủ các khóa '
            '"id", "severity", "code", "message", "suggestion"; severity chỉ là '
            '"warning" hoặc "blocker".',
            '"suggestion" chỉ được chứa nguyên văn câu tiếng Việt thay thế hoàn chỉnh, '
            'không chứa lời giải thích/hướng dẫn; nếu chưa có câu thay thế chắc chắn thì để "".',
            _style_prompt(film_bible),
            "ID phải là số id nguyên văn của chính cue bị lỗi trong CUES, không đánh số thứ tự "
            'issue và không dùng ID minh họa. Nếu không có lỗi, trả {"issues":[]}.',
            "ALLOWED_IDS=" + json.dumps(list(chunk.ids)),
            "FILM_BIBLE=" + json.dumps(_film_bible_payload(film_bible), ensure_ascii=False),
            "CUES=" + json.dumps(rows, ensure_ascii=False),
        )
        if part
    )
    return TranslationRequest(
        request_id=f"qa-{chunk.index}",
        purpose=TranslationPurpose.QA,
        prompt=prompt,
        expected_ids=chunk.ids,
        temperature=0.0,
        response_schema=_qa_response_schema(),
    )


def parse_json_object(raw: str) -> dict[str, object]:
    text = _FENCE.sub("", raw.strip()).strip()
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end < start:
        raise ValueError("Gemini response does not contain a JSON object")
    candidate = _TRAILING_COMMA.sub(r"\1", text[start : end + 1])
    value = json.loads(candidate)
    if not isinstance(value, dict):
        raise ValueError("Gemini response root must be an object")
    return value


def parse_translation_response(
    raw: str,
    *,
    expected_cues: Sequence[SubtitleCue],
) -> TranslationChunkResult:
    payload = parse_json_object(raw)
    rows = payload.get("translations")
    if not isinstance(rows, list):
        raise ValueError("Gemini response translations must be an array")
    expected = [cue.index for cue in expected_cues]
    id_to_cue = {cue.index: cue for cue in expected_cues}
    received: list[int] = []
    values: list[TranslationValue] = []
    for row in rows:
        if not isinstance(row, dict) or type(row.get("id")) is not int:
            raise ValueError("Every translated cue requires an integer id")
        index = int(row["id"])
        text = row.get("text")
        if not isinstance(text, str) or not text.strip():
            raise ValueError(f"Translated cue {index} is empty")
        if index not in id_to_cue:
            raise ValueError(f"Gemini returned unexpected cue id {index}")
        received.append(index)
        values.append(TranslationValue(id_to_cue[index].id, index, text.strip()))
    if received != expected or len(received) != len(set(received)):
        missing = [value for value in expected if value not in received]
        raise ValueError(
            "Gemini cue coverage mismatch; "
            f"expected={expected}, received={received}, missing={missing}"
        )
    glossary_rows = payload.get("glossary_updates", [])
    if not isinstance(glossary_rows, list):
        raise ValueError("Gemini glossary_updates must be an array")
    updates: list[FilmTerm] = []
    for row in glossary_rows:
        if not isinstance(row, dict):
            raise ValueError("Every glossary update must be an object")
        source = row.get("source")
        vietnamese = row.get("vi")
        kind = row.get("kind", "term")
        if not all(isinstance(item, str) for item in (source, vietnamese, kind)):
            raise ValueError("Glossary updates require string fields")
        if contains_cjk(vietnamese):
            continue
        updates.append(FilmTerm(source, vietnamese, kind))
    try:
        issues = parse_qa_response(
            json.dumps({"issues": payload.get("issues", [])}, ensure_ascii=False),
            allowed_ids=expected,
        )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        # Translation rows are a separate contract from the model's optional
        # integrated QA rows. Preserve a complete, ordered translation payload,
        # but fail closed by sending every cue in this chunk to review/repair.
        issues = tuple(
            TranslationQaIssue(
                index,
                "malformed_integrated_qa",
                TranslationQaSeverity.BLOCKER,
                f"QA tích hợp trả sai định dạng: {str(exc)[:180]}",
            )
            for index in expected
        )
    return TranslationChunkResult(tuple(values), tuple(updates), issues)


def local_translation_qa(
    values: Sequence[TranslationValue],
    *,
    banned_latin_terms: Sequence[str] = (),
    long_line_chars: int = 72,
    scene_text_ids: Iterable[int] = (),
) -> tuple[TranslationQaIssue, ...]:
    issues: list[TranslationQaIssue] = []
    banned = tuple(term.strip() for term in banned_latin_terms if term.strip())
    scene_ids = frozenset(scene_text_ids)
    for value in values:
        text = value.text.strip()
        if _CJK.search(text):
            issues.append(
                TranslationQaIssue(
                    value.index,
                    "cjk_remaining",
                    TranslationQaSeverity.BLOCKER,
                    "Bản dịch còn chữ Hán/CJK.",
                )
            )
        for term in banned:
            if re.search(rf"(?<!\w){re.escape(term)}(?!\w)", text, re.IGNORECASE):
                issues.append(
                    TranslationQaIssue(
                        value.index,
                        "untranslated_latin_term",
                        TranslationQaSeverity.BLOCKER,
                        f"Bản dịch còn thuật ngữ Latin chưa Việt hóa: {term}.",
                    )
                )
        longest_line = max((len(line) for line in text.splitlines()), default=0)
        if value.index in scene_ids:
            continue
        if longest_line > long_line_chars * 2:
            issues.append(
                TranslationQaIssue(
                    value.index,
                    "overlong_translation",
                    TranslationQaSeverity.BLOCKER,
                    "Câu dịch quá dài để hiển thị và tạo TTS an toàn; cần diễn đạt gọn hơn.",
                )
            )
        elif longest_line > long_line_chars:
            issues.append(
                TranslationQaIssue(
                    value.index,
                    "long_line",
                    TranslationQaSeverity.WARNING,
                    "Dòng phụ đề dài, nên xuống dòng để đọc và tạo TTS ổn định hơn.",
                )
            )
        if len(text) >= 18 and not _VIETNAMESE_MARK.search(text):
            ascii_words = re.findall(r"[A-Za-z]{3,}", text)
            if len(ascii_words) >= 4:
                issues.append(
                    TranslationQaIssue(
                        value.index,
                        "possibly_untranslated_latin",
                        TranslationQaSeverity.WARNING,
                        "Câu có nhiều từ Latin nhưng không có dấu tiếng Việt; cần kiểm tra.",
                    )
                )
    return tuple(issues)


def film_bible_translation_qa(
    chunk: TranslationChunk,
    values: Sequence[TranslationValue],
    *,
    film_bible: FilmBible,
) -> tuple[TranslationQaIssue, ...]:
    """Fast deterministic consistency checks for the integrated QA path."""

    translated_by_index = {value.index: value for value in values}
    issues: list[TranslationQaIssue] = []
    for cue in chunk.cues:
        value = translated_by_index.get(cue.index)
        if value is None:
            continue
        for term in film_bible.terms:
            source = term.source.strip()
            if (
                not source
                or source not in cue.text
                or _contains_normalized_phrase(value.text, term.vietnamese)
            ):
                continue
            kind = term.kind.strip().casefold()
            is_identity = kind in {"name", "place"}
            issues.append(
                TranslationQaIssue(
                    cue.index,
                    "film_bible_name_mismatch" if is_identity else "film_bible_term_mismatch",
                    (
                        TranslationQaSeverity.BLOCKER
                        if is_identity
                        else TranslationQaSeverity.WARNING
                    ),
                    (
                        f"Nguồn có '{source}' nhưng bản dịch không dùng cách gọi "
                        f"'{term.vietnamese}' trong Film Bible."
                    ),
                )
            )
    return tuple(issues)


def translation_style_qa(
    chunk: TranslationChunk,
    values: Sequence[TranslationValue],
    *,
    film_bible: FilmBible,
) -> tuple[TranslationQaIssue, ...]:
    """Detect high-confidence style violations without rewriting user text.

    The source cue is required for alias checks, which keeps ordinary animal
    words and insults out of the Sino-Vietnamese conversion rule.
    """

    if film_bible.resolved_style not in _ANCIENT_STYLE_PRESETS:
        return ()
    source_by_index = {cue.index: cue.text for cue in chunk.cues}
    issues: list[TranslationQaIssue] = []
    modern_pronouns = re.compile(
        r"(?<!\w)(?:mày|tao)(?!\w)|"
        r"(?<!\w)(?:anh|em)(?!\s+(?:trai|gái|cả|hai|ba|hùng|bé|nhỏ)(?!\w))(?!\w)",
        re.IGNORECASE,
    )
    reversed_title = re.compile(
        r"(?<!\w)(?:tỷ\s+tỷ|sư\s+tỷ|sư\s+huynh)\s+"
        r"(?P<name>[^\W\d_]+)",
        re.IGNORECASE,
    )
    aliases = (("阿狗", "A Cẩu"), ("阿猫", "A Miêu"), ("老狗", "Lão Cẩu"))
    for value in values:
        text = " ".join(value.text.split())
        if modern_pronouns.search(text):
            issues.append(
                TranslationQaIssue(
                    value.index,
                    "ancient_modern_pronoun",
                    TranslationQaSeverity.BLOCKER,
                    "Xưng hô hiện đại chưa phù hợp với văn phong cổ trang; cần sửa theo vai vế.",
                )
            )
        reversed_title_match = reversed_title.search(text)
        if (
            reversed_title_match is not None
            and reversed_title_match.group("name")[:1].isupper()
        ):
            issues.append(
                TranslationQaIssue(
                    value.index,
                    "ancient_title_order",
                    TranslationQaSeverity.BLOCKER,
                    "Danh xưng cổ trang đang đứng trước tên; hãy đặt tên riêng trước hậu tố, "
                    "ví dụ Hồng Ly tỷ tỷ.",
                )
            )
        source = source_by_index.get(value.index, "")
        for chinese, vietnamese in aliases:
            if chinese not in source or _contains_normalized_phrase(text, vietnamese):
                continue
            issues.append(
                TranslationQaIssue(
                    value.index,
                    "ancient_alias_mismatch",
                    TranslationQaSeverity.BLOCKER,
                    f"Biệt danh nguồn '{chinese}' cần dùng cách gọi '{vietnamese}' trong "
                    "ngữ cảnh cổ trang.",
                )
            )
    return tuple(issues)


def downgrade_style_qa_issues(
    issues: Sequence[TranslationQaIssue],
) -> tuple[TranslationQaIssue, ...]:
    """After the single remote style repair, keep evidence without blocking."""

    return tuple(
        replace(
            issue,
            severity=TranslationQaSeverity.WARNING,
            original_severity=issue.original_severity or TranslationQaSeverity.BLOCKER,
            message=issue.message + " Đã thử sửa văn phong một lần; cần rà soát thủ công.",
        )
        if issue.code in STYLE_QA_CODES
        and issue.severity is TranslationQaSeverity.BLOCKER
        else issue
        for issue in issues
    )


def wrap_vietnamese_subtitle(
    text: str,
    *,
    max_chars: int = VIETNAMESE_SUBTITLE_WRAP_CHARS,
) -> str:
    """Wrap at spaces without changing or dropping any translated word."""
    normalized = " ".join(text.replace("\r", " ").replace("\n", " ").split())
    if not normalized or len(normalized) <= max_chars:
        return normalized
    words = normalized.split(" ")
    lines: list[str] = []
    current: list[str] = []
    for word in words:
        candidate = " ".join((*current, word))
        if current and len(candidate) > max_chars:
            lines.append(" ".join(current))
            current = [word]
        else:
            current.append(word)
    if current:
        lines.append(" ".join(current))
    if len(lines) <= 2:
        return "\n".join(lines)
    target = len(normalized) / 2
    best_index = min(
        range(1, len(words)),
        key=lambda index: abs(len(" ".join(words[:index])) - target),
    )
    return " ".join(words[:best_index]) + "\n" + " ".join(words[best_index:])


def parse_qa_response(
    raw: str,
    *,
    allowed_ids: Sequence[int],
) -> tuple[TranslationQaIssue, ...]:
    payload = parse_json_object(raw)
    rows = payload.get("issues")
    if not isinstance(rows, list):
        raise ValueError("Gemini QA issues must be an array")
    allowed = set(allowed_ids)
    issues: list[TranslationQaIssue] = []
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("Every QA issue requires an integer id")
        evidence = " ".join(str(row.get(field, "")) for field in ("message", "suggestion"))
        referenced = {
            int(match)
            for match in re.findall(r"(?i)(?:\bcue\b|\bid\b)\s*[:#-]?\s*(\d+)", evidence)
            if int(match) in allowed
        }
        raw_index = row.get("id")
        if type(raw_index) is int:
            index = int(raw_index)
        elif len(referenced) == 1:
            index = referenced.pop()
        elif len(allowed) == 1:
            # Targeted repair contains one cue, so Gemini omitting the
            # redundant QA id is unambiguous and safe to recover.
            index = next(iter(allowed))
        else:
            raise ValueError("Every QA issue requires an integer id")
        if index not in allowed:
            if len(referenced) == 1:
                index = referenced.pop()
            elif len(allowed) == 1:
                index = next(iter(allowed))
        if index not in allowed:
            raise ValueError(f"Gemini QA returned unexpected cue id {index}")
        raw_severity = str(row.get("severity", "")).strip().casefold()
        severity_aliases = {
            "block": TranslationQaSeverity.BLOCKER,
            "blocker": TranslationQaSeverity.BLOCKER,
            "critical": TranslationQaSeverity.BLOCKER,
            "error": TranslationQaSeverity.BLOCKER,
            "high": TranslationQaSeverity.BLOCKER,
            "info": TranslationQaSeverity.WARNING,
            "low": TranslationQaSeverity.WARNING,
            "medium": TranslationQaSeverity.WARNING,
            "warn": TranslationQaSeverity.WARNING,
            "warning": TranslationQaSeverity.WARNING,
        }
        try:
            severity = severity_aliases[raw_severity]
        except KeyError:
            # Gemini often changes casing or uses common severity synonyms.
            # Normalize those without accepting arbitrary values, which would
            # silently weaken the QA contract.
            raise ValueError(f"Gemini QA returned invalid severity for cue {index}") from None
        code = row.get("code")
        message = row.get("message")
        suggestion = row.get("suggestion", "")
        if not all(isinstance(item, str) for item in (code, message, suggestion)):
            raise ValueError("Gemini QA issue fields must be strings")
        if contains_cjk(suggestion):
            suggestion = ""
        issues.append(TranslationQaIssue(index, code, severity, message, suggestion))
    return tuple(issues)


def reopen_qa_reviews_for_resume(ledger: TranslationLedger) -> TranslationLedger:
    """Re-run every review on resume; cached translations remain intact."""
    changed = False
    chunks: list[ChunkLedgerEntry] = []
    for chunk in ledger.chunks:
        if chunk.state is ChunkState.QA_REVIEW:
            chunks.append(
                replace(
                    chunk,
                    state=ChunkState.PENDING,
                    attempts=0,
                    response_fingerprint="",
                )
            )
            changed = True
        else:
            chunks.append(chunk)
    return replace(ledger, chunks=tuple(chunks)) if changed else ledger


def replan_ledger_for_resume(
    ledger: TranslationLedger,
    cues: Sequence[SubtitleCue],
    *,
    max_cues: int,
    max_source_chars: int,
) -> tuple[TranslationLedger, tuple[TranslationChunk, ...]]:
    """Reopen only review chunks while preserving every verified cache chunk.

    A previous implementation rebuilt the entire suffix after the first review
    blocker.  One blocker near the start therefore caused dozens of later
    ``QA_PASS`` chunks to be checked again.  Existing chunk boundaries are part
    of the durable ledger and remain valid even when current UI chunk limits
    change; only ``QA_REVIEW`` entries need to become pending again.
    """
    if max_cues <= 0 or max_source_chars <= 0:
        raise ValueError("Translation resume chunk limits are invalid")
    source_ids = tuple(cue.index for cue in cues)
    if source_ids != tuple(range(1, len(cues) + 1)):
        raise ValueError("Translation resume source cue indices are not contiguous")
    ledger_ids = tuple(cue_id for chunk in ledger.chunks for cue_id in chunk.cue_ids)
    if ledger_ids != source_ids:
        raise ValueError("Translation resume ledger does not cover the source cues")
    if not set(ledger.translations).issubset(source_ids):
        raise ValueError("Translation resume ledger contains unknown cached cue IDs")

    cue_by_index = {cue.index: cue for cue in cues}
    for entry in ledger.chunks:
        if entry.state is ChunkState.QA_PASS and any(
            cue_id not in ledger.translations for cue_id in entry.cue_ids
        ):
            raise ValueError("Translation QA-pass chunk has missing cached text")
    replanned = reopen_qa_reviews_for_resume(ledger)
    chunks = tuple(
        TranslationChunk(
            entry.chunk_index,
            (
                SemanticGroup(
                    f"cached-{entry.chunk_index + 1}",
                    tuple(cue_by_index[cue_id] for cue_id in entry.cue_ids),
                ),
            ),
        )
        for entry in replanned.chunks
    )
    if tuple(index for chunk in chunks for index in chunk.ids) != source_ids:
        raise ValueError("Translation resume re-plan lost source cue coverage")
    return replanned, chunks


def mark_chunk_complete(
    ledger: TranslationLedger,
    *,
    chunk_index: int,
    values: Sequence[TranslationValue],
    qa_issues: Sequence[TranslationQaIssue],
    film_bible: FilmBible,
    attempts: int,
) -> TranslationLedger:
    if not 0 <= chunk_index < len(ledger.chunks):
        raise IndexError("Unknown translation chunk")
    entry = ledger.chunks[chunk_index]
    received = tuple(value.index for value in values)
    if received != entry.cue_ids:
        raise ValueError("Completed translation chunk does not match ledger ids")
    blockers = [item for item in qa_issues if item.severity is TranslationQaSeverity.BLOCKER]
    updated_entry = replace(
        entry,
        state=ChunkState.QA_REVIEW if blockers else ChunkState.QA_PASS,
        attempts=attempts,
        response_fingerprint=stable_fingerprint(
            [{"id": value.index, "text": value.text} for value in values]
        ),
        qa_issues=tuple(qa_issues),
    )
    chunks = list(ledger.chunks)
    chunks[chunk_index] = updated_entry
    translated = {int(key): str(value) for key, value in ledger.translations.items()}
    translated.update({value.index: value.text for value in values})
    return replace(
        ledger,
        chunks=tuple(chunks),
        translations=translated,
        film_bible=film_bible,
    )


def ledger_to_dict(ledger: TranslationLedger) -> dict[str, JsonValue]:
    return {
        "schema_version": 1,
        "signature": ledger.signature,
        "provider_id": ledger.provider_id,
        "model_id": ledger.model_id,
        "film_bible": _film_bible_payload(ledger.film_bible),
        "chunks": [
            {
                "chunk_index": item.chunk_index,
                "cue_ids": list(item.cue_ids),
                "state": item.state.value,
                "attempts": item.attempts,
                "response_fingerprint": item.response_fingerprint,
                "qa_issues": [
                    {
                        "id": issue.index,
                        "code": issue.code,
                        "severity": issue.severity.value,
                        "original_severity": issue.original_severity.value
                        if issue.original_severity is not None
                        else issue.severity.value,
                        "message": issue.message,
                        "suggestion": issue.suggestion,
                    }
                    for issue in item.qa_issues
                ],
            }
            for item in ledger.chunks
        ],
        "translations": {str(key): value for key, value in ledger.translations.items()},
    }


def ledger_from_dict(raw: Mapping[str, object]) -> TranslationLedger:
    if raw.get("schema_version") != 1:
        raise ValueError("Unsupported translation ledger schema")
    bible_raw = raw.get("film_bible", {})
    if not isinstance(bible_raw, Mapping):
        raise ValueError("Translation ledger Film Bible is invalid")
    raw_terms = bible_raw.get("terms", [])
    if not isinstance(raw_terms, Sequence) or isinstance(raw_terms, (str, bytes)):
        raise ValueError("Translation ledger terms are invalid")
    terms: list[FilmTerm] = []
    for item in raw_terms:
        if not isinstance(item, Mapping):
            continue
        source = str(item.get("source", ""))
        vietnamese = str(item.get("vi", ""))
        kind = str(item.get("kind", "term"))
        if contains_cjk(vietnamese):
            continue
        terms.append(FilmTerm(source, vietnamese, kind))
    raw_characters = bible_raw.get("characters", [])
    if not isinstance(raw_characters, Sequence) or isinstance(raw_characters, (str, bytes)):
        raise ValueError("Translation ledger characters are invalid")
    bible = FilmBible(
        str(bible_raw.get("title", "")),
        str(bible_raw.get("profile", "neutral")),
        ""
        if contains_cjk(str(bible_raw.get("synopsis", "")))
        else str(bible_raw.get("synopsis", "")),
        tuple(terms),
        tuple(str(value) for value in raw_characters if not contains_cjk(str(value))),
        str(bible_raw.get("translation_style", {}).get("preset", "legacy"))
        if isinstance(bible_raw.get("translation_style"), Mapping)
        else "legacy",
        str(bible_raw.get("translation_style", {}).get("custom_rules", ""))
        if isinstance(bible_raw.get("translation_style"), Mapping)
        else "",
    )
    raw_chunks = raw.get("chunks")
    if not isinstance(raw_chunks, Sequence) or isinstance(raw_chunks, (str, bytes)):
        raise ValueError("Translation ledger chunks are invalid")
    chunks: list[ChunkLedgerEntry] = []
    for raw_chunk in raw_chunks:
        if not isinstance(raw_chunk, Mapping):
            raise ValueError("Translation ledger chunk is invalid")
        raw_issues = raw_chunk.get("qa_issues", [])
        if not isinstance(raw_issues, Sequence) or isinstance(raw_issues, (str, bytes)):
            raise ValueError("Translation ledger QA issues are invalid")
        issues = tuple(
            TranslationQaIssue(
                int(issue["id"]),
                str(issue["code"]),
                TranslationQaSeverity(str(issue["severity"])),
                str(issue["message"]),
                str(issue.get("suggestion", "")),
                TranslationQaSeverity(
                    str(issue.get("original_severity", issue.get("severity", "warning")))
                ),
            )
            for issue in raw_issues
            if isinstance(issue, Mapping)
        )
        cue_ids = raw_chunk.get("cue_ids")
        if not isinstance(cue_ids, Sequence) or isinstance(cue_ids, (str, bytes)):
            raise ValueError("Translation ledger cue ids are invalid")
        chunks.append(
            ChunkLedgerEntry(
                int(raw_chunk["chunk_index"]),
                tuple(int(value) for value in cue_ids),
                ChunkState(str(raw_chunk.get("state", "pending"))),
                int(raw_chunk.get("attempts", 0)),
                str(raw_chunk.get("response_fingerprint", "")),
                issues,
            )
        )
    raw_translations = raw.get("translations", {})
    if not isinstance(raw_translations, Mapping):
        raise ValueError("Translation ledger translations are invalid")
    return TranslationLedger(
        str(raw["signature"]),
        str(raw["provider_id"]),
        str(raw["model_id"]),
        tuple(chunks),
        {int(key): str(value) for key, value in raw_translations.items()},
        bible,
    )


def summarize_qa(ledger: TranslationLedger) -> dict[str, JsonValue]:
    issues = [issue for chunk in ledger.chunks for issue in chunk.qa_issues]
    counts = Counter(issue.code for issue in issues)
    blockers = [issue for issue in issues if issue.severity is TranslationQaSeverity.BLOCKER]
    warnings = [issue for issue in issues if issue.severity is TranslationQaSeverity.WARNING]
    return {
        "status": "PASS" if ledger.complete and not blockers else "REVIEW",
        "cue_count": len(ledger.translations),
        "blocking_count": len(blockers),
        "warning_count": len(warnings),
        "reason_counts": dict(counts),
        "issues": [
            {
                "id": issue.index,
                "code": issue.code,
                "severity": issue.severity.value,
                "original_severity": issue.original_severity.value
                if issue.original_severity is not None
                else issue.severity.value,
                "message": issue.message,
                "suggestion": issue.suggestion,
            }
            for issue in issues
        ],
    }


def translation_qa_snapshot(
    ledger: TranslationLedger,
    cues: Sequence[SubtitleCue],
) -> dict[str, JsonValue]:
    """Return the live, cue-scoped blocker state for editor navigation.

    ``summarize_qa`` is the final publication contract and counts individual
    issues.  The editor needs a different view: one row per failing cue, plus a
    truthful pending state while not every chunk has completed QA.
    """

    cue_by_index = {cue.index: cue for cue in cues}
    issues_by_index: dict[int, list[TranslationQaIssue]] = {}
    warnings_by_index: dict[int, list[TranslationQaIssue]] = {}
    warning_count = 0
    for chunk in ledger.chunks:
        for issue in chunk.qa_issues:
            if issue.severity is TranslationQaSeverity.BLOCKER:
                issues_by_index.setdefault(issue.index, []).append(issue)
            else:
                warnings_by_index.setdefault(issue.index, []).append(issue)
                warning_count += 1

    rows: list[dict[str, JsonValue]] = []
    blocker_issue_count = 0
    for cue_index in sorted(issues_by_index):
        cue = cue_by_index.get(cue_index)
        if cue is None:
            continue
        cue_issues = issues_by_index[cue_index]
        blocker_issue_count += len(cue_issues)
        codes = list(dict.fromkeys(issue.code for issue in cue_issues))
        messages = list(dict.fromkeys(issue.message for issue in cue_issues))
        suggestion = next(
            (issue.suggestion for issue in cue_issues if issue.suggestion.strip()),
            "",
        )
        rows.append(
            {
                "id": cue_index,
                "cue_id": cue.id,
                "start_us": cue.range.start_us,
                "end_us": cue.range.end_us,
                "source_text": cue.text,
                "translated_text": str(ledger.translations.get(cue_index, "")),
                "code": ", ".join(codes),
                "codes": codes,
                "message": "\n".join(messages),
                "suggestion": suggestion,
                "issue_count": len(cue_issues),
                "details": [
                    {
                        "code": issue.code,
                        "severity": issue.severity.value,
                        "original_severity": issue.original_severity.value
                        if issue.original_severity is not None
                        else issue.severity.value,
                        "message": issue.message,
                        "suggestion": issue.suggestion,
                    }
                    for issue in cue_issues
                ],
            }
        )

    checked_cues = sum(
        len(chunk.cue_ids)
        for chunk in ledger.chunks
        if chunk.state in {ChunkState.QA_PASS, ChunkState.QA_REVIEW} or chunk.qa_issues
    )
    cue_total = sum(len(chunk.cue_ids) for chunk in ledger.chunks)
    status = (
        "PASS"
        if ledger.complete and not rows
        else ("REVIEW" if rows else "PENDING")
    )
    return {
        "status": status,
        "completed_cues": len(ledger.translations),
        "checked_cues": checked_cues,
        "cue_total": cue_total,
        "blocking_count": len(rows),
        "blocking_issue_count": blocker_issue_count,
        "warning_count": warning_count,
        "content_warnings": [
            {
                "id": cue_index,
                "cue_id": cue_by_index[cue_index].id,
                "start_us": cue_by_index[cue_index].range.start_us,
                "end_us": cue_by_index[cue_index].range.end_us,
                "message": "\n".join(dict.fromkeys(issue.message for issue in cue_issues)),
                "codes": list(dict.fromkeys(issue.code for issue in cue_issues)),
                "details": [
                    {
                        "code": issue.code,
                        "severity": issue.severity.value,
                        "original_severity": issue.original_severity.value
                        if issue.original_severity is not None
                        else issue.severity.value,
                        "message": issue.message,
                        "suggestion": issue.suggestion,
                    }
                    for issue in cue_issues
                ],
            }
            for cue_index, cue_issues in sorted(warnings_by_index.items())
            if cue_index in cue_by_index
        ],
        "issues": rows,
    }
