from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from dataclasses import replace
from uuid import uuid4

from ducai_studio.domain.project import ProjectSnapshot, SubtitleCue
from ducai_studio.domain.timebase import TimeRange


class SubtitleEditError(ValueError):
    pass


def auto_break_text(text: str, *, max_chars: int) -> str:
    if max_chars < 8:
        raise ValueError("Subtitle line length must be at least 8 characters")
    value = " ".join(text.replace("\n", " ").split())
    if len(value) <= max_chars:
        return value
    lines: list[str] = []
    remaining = value
    break_characters = " ，。！？；：、,.!?;:"
    while len(remaining) > max_chars:
        lower = max(1, int(max_chars * 0.55))
        positions = [
            index + 1
            for index, character in enumerate(remaining[: max_chars + 1])
            if index + 1 >= lower and character in break_characters
        ]
        split_at = positions[-1] if positions else max_chars
        lines.append(remaining[:split_at].strip())
        remaining = remaining[split_at:].strip()
    if remaining:
        lines.append(remaining)
    return "\n".join(line for line in lines if line)


class SubtitleEditor:
    def __init__(
        self,
        snapshot: ProjectSnapshot,
        *,
        id_factory: Callable[[], str] | None = None,
    ) -> None:
        self.snapshot = snapshot
        self._undo: list[ProjectSnapshot] = []
        self._redo: list[ProjectSnapshot] = []
        self._id_factory = id_factory or (lambda: uuid4().hex)

    @property
    def can_undo(self) -> bool:
        return bool(self._undo)

    @property
    def can_redo(self) -> bool:
        return bool(self._redo)

    def _new_id(self) -> str:
        value = self._id_factory().strip()
        if not value:
            raise RuntimeError("Subtitle id factory returned a blank value")
        return f"cue-{value}"

    def _cues(self, language: str) -> tuple[SubtitleCue, ...]:
        return self.snapshot.source_cues if language == "source" else self.snapshot.translated_cues

    def _replace_cues(
        self, language: str, cues: Iterable[SubtitleCue]
    ) -> ProjectSnapshot:
        normalized = self._normalize_cues(cues)
        if language == "source":
            return replace(self.snapshot, source_cues=normalized)
        if language == "translated":
            return replace(self.snapshot, translated_cues=normalized)
        raise SubtitleEditError("Subtitle language must be source or translated")

    @staticmethod
    def _normalize_cues(cues: Iterable[SubtitleCue]) -> tuple[SubtitleCue, ...]:
        return tuple(
            replace(cue, index=index)
            for index, cue in enumerate(
                sorted(cues, key=lambda item: (item.range.start_us, item.range.end_us, item.id)),
                start=1,
            )
        )

    def _commit(self, candidate: ProjectSnapshot) -> ProjectSnapshot:
        if candidate.id != self.snapshot.id:
            raise SubtitleEditError("Subtitle command cannot change project identity")
        previous = self.snapshot
        self.snapshot = replace(candidate, revision=previous.revision + 1)
        self._undo.append(previous)
        self._redo.clear()
        return self.snapshot

    def _index(self, cues: tuple[SubtitleCue, ...], cue_id: str) -> int:
        for index, cue in enumerate(cues):
            if cue.id == cue_id:
                return index
        raise LookupError(f"Unknown subtitle cue: {cue_id}")

    @staticmethod
    def _assert_timeline(cues: Iterable[SubtitleCue]) -> None:
        previous: SubtitleCue | None = None
        for cue in sorted(cues, key=lambda item: item.range.start_us):
            if previous is not None and previous.range.end_us > cue.range.start_us:
                raise SubtitleEditError("Subtitle cues cannot overlap")
            previous = cue

    def add(
        self,
        *,
        language: str = "source",
        after_cue_id: str | None = None,
        text: str = "Cue mới",
    ) -> ProjectSnapshot:
        cues = self._cues(language)
        if after_cue_id:
            index = self._index(cues, after_cue_id)
            start_us = cues[index].range.end_us
            next_start = (
                cues[index + 1].range.start_us
                if index + 1 < len(cues)
                else start_us + 2_000_000
            )
            end_us = min(start_us + 2_000_000, next_start)
            if end_us <= start_us:
                raise SubtitleEditError("No timeline room after the selected cue")
        elif cues:
            start_us = cues[-1].range.end_us
            end_us = start_us + 2_000_000
        else:
            start_us, end_us = 0, 2_000_000
        language_code = "zh" if language == "source" else "vi"
        result = (
            *cues,
            SubtitleCue(
                self._new_id(), 1, TimeRange(start_us, end_us), text, language_code
            ),
        )
        self._assert_timeline(result)
        return self._commit(self._replace_cues(language, result))

    def update_text(self, cue_id: str, text: str, *, language: str = "source") -> ProjectSnapshot:
        if not text.strip():
            raise SubtitleEditError("Subtitle text cannot be empty")
        cues = list(self._cues(language))
        index = self._index(tuple(cues), cue_id)
        cues[index] = replace(cues[index], text=text.strip(), revision=cues[index].revision + 1)
        return self._commit(self._replace_cues(language, cues))

    def split(
        self,
        cue_id: str,
        *,
        split_us: int | None = None,
        language: str = "source",
    ) -> ProjectSnapshot:
        cues = list(self._cues(language))
        index = self._index(tuple(cues), cue_id)
        cue = cues[index]
        target_us = split_us if split_us is not None else (
            cue.range.start_us + cue.range.end_us
        ) // 2
        if type(target_us) is not int or not cue.range.start_us < target_us < cue.range.end_us:
            raise SubtitleEditError("Subtitle split point must be inside the cue")
        text = cue.text.replace("\n", " ").strip()
        midpoint = max(1, len(text) // 2)
        punctuation = [
            match.end()
            for match in re.finditer(r"[，。！？；：、,.!?;:]|\s+", text)
            if 1 <= match.end() < len(text)
        ]
        split_at = (
            min(punctuation, key=lambda value: abs(value - midpoint))
            if punctuation
            else midpoint
        )
        left_text = text[:split_at].strip()
        right_text = text[split_at:].strip()
        if not left_text or not right_text:
            raise SubtitleEditError("Subtitle text cannot be split into two non-empty cues")
        left = replace(
            cue,
            range=TimeRange(cue.range.start_us, target_us),
            text=left_text,
            revision=cue.revision + 1,
        )
        right = replace(
            cue,
            id=self._new_id(),
            range=TimeRange(target_us, cue.range.end_us),
            text=right_text,
            revision=1,
        )
        cues[index : index + 1] = [left, right]
        return self._commit(self._replace_cues(language, cues))

    def merge_with_next(self, cue_id: str, *, language: str = "source") -> ProjectSnapshot:
        cues = list(self._cues(language))
        index = self._index(tuple(cues), cue_id)
        if index + 1 >= len(cues):
            raise SubtitleEditError("Selected cue has no following cue")
        left, right = cues[index : index + 2]
        separator = "" if left.language.startswith("zh") else " "
        merged = replace(
            left,
            range=TimeRange(left.range.start_us, right.range.end_us),
            text=(left.text.rstrip() + separator + right.text.lstrip()).strip(),
            revision=left.revision + 1,
            evidence_ids=tuple(dict.fromkeys((*left.evidence_ids, *right.evidence_ids))),
        )
        cues[index : index + 2] = [merged]
        return self._commit(self._replace_cues(language, cues))

    def adjust_time(
        self,
        cue_id: str,
        *,
        start_us: int,
        end_us: int,
        language: str = "source",
    ) -> ProjectSnapshot:
        cues = list(self._cues(language))
        index = self._index(tuple(cues), cue_id)
        cues[index] = replace(
            cues[index],
            range=TimeRange(start_us, end_us),
            revision=cues[index].revision + 1,
        )
        self._assert_timeline(cues)
        return self._commit(self._replace_cues(language, cues))

    def shift(self, cue_id: str, delta_us: int, *, language: str = "source") -> ProjectSnapshot:
        if type(delta_us) is not int:
            raise TypeError("Subtitle shift must use integer microseconds")
        cues = self._cues(language)
        cue = cues[self._index(cues, cue_id)]
        return self.adjust_time(
            cue_id,
            start_us=cue.range.start_us + delta_us,
            end_us=cue.range.end_us + delta_us,
            language=language,
        )

    def auto_break(
        self,
        *,
        language: str = "source",
        cue_id: str | None = None,
        max_chars: int | None = None,
    ) -> ProjectSnapshot:
        limit = max_chars or (20 if language == "source" else 42)
        cues = list(self._cues(language))
        changed = False
        for index, cue in enumerate(cues):
            if cue_id is not None and cue.id != cue_id:
                continue
            text = auto_break_text(cue.text, max_chars=limit)
            if text != cue.text:
                cues[index] = replace(cue, text=text, revision=cue.revision + 1)
                changed = True
        if not changed:
            return self.snapshot
        return self._commit(self._replace_cues(language, cues))

    def find_replace(
        self,
        find: str,
        replacement: str,
        *,
        language: str = "source",
        expected_count: int | None = None,
    ) -> ProjectSnapshot:
        if not find:
            raise SubtitleEditError("Find text cannot be empty")
        match_count, _ = self.count_matches(find, language=language)
        if expected_count is not None and match_count != expected_count:
            raise SubtitleEditError(
                "Nội dung phụ đề đã thay đổi sau khi đếm. Hãy bấm Thay tất cả lại để kiểm tra."
            )
        cues = list(self._cues(language))
        changed = False
        for index, cue in enumerate(cues):
            text = cue.text.replace(find, replacement)
            if text != cue.text:
                if not text.strip():
                    raise SubtitleEditError("Find/replace cannot empty a cue")
                cues[index] = replace(cue, text=text, revision=cue.revision + 1)
                changed = True
        if not changed:
            return self.snapshot
        return self._commit(self._replace_cues(language, cues))

    def count_matches(self, find: str, *, language: str = "source") -> tuple[int, int]:
        if not find:
            raise SubtitleEditError("Find text cannot be empty")
        counts = [cue.text.count(find) for cue in self._cues(language)]
        return sum(counts), sum(count > 0 for count in counts)

    def delete(self, cue_id: str) -> ProjectSnapshot:
        source = list(self.snapshot.source_cues)
        translated = list(self.snapshot.translated_cues)
        matched = next(
            (cue for cue in (*source, *translated) if cue.id == cue_id),
            None,
        )
        if matched is None:
            raise LookupError(f"Unknown subtitle cue: {cue_id}")

        cue_index = matched.index
        source = [cue for cue in source if cue.index != cue_index]
        translated = [cue for cue in translated if cue.index != cue_index]
        self._assert_timeline(source)
        self._assert_timeline(translated)
        candidate = replace(
            self.snapshot,
            source_cues=self._normalize_cues(source),
            translated_cues=self._normalize_cues(translated),
        )
        return self._commit(candidate)

    def apply_ocr_rescan(
        self,
        cue_id: str,
        *,
        text: str,
        confidence: float,
        evidence_ids: tuple[str, ...],
        source: str,
    ) -> ProjectSnapshot:
        if not text.strip() or not source.strip() or not evidence_ids:
            raise SubtitleEditError("OCR rescan requires text, source and evidence")
        if not 0 <= confidence <= 1:
            raise SubtitleEditError("OCR rescan confidence must be between zero and one")
        cues = list(self.snapshot.source_cues)
        index = self._index(tuple(cues), cue_id)
        cues[index] = replace(
            cues[index],
            text=text.strip(),
            confidence=confidence,
            evidence_ids=tuple(dict.fromkeys(evidence_ids)),
            source=source,
            revision=cues[index].revision + 1,
        )
        return self._commit(self._replace_cues("source", cues))

    def undo(self) -> ProjectSnapshot:
        if not self._undo:
            raise SubtitleEditError("No subtitle edit to undo")
        previous = self._undo.pop()
        current = self.snapshot
        self._redo.append(current)
        # Revisions are immutable and strictly monotonic in ProjectRepository.
        # Undo restores the previous content, but publishes it as a new
        # revision instead of moving the project head backwards.
        self.snapshot = replace(previous, revision=current.revision + 1)
        return self.snapshot

    def redo(self) -> ProjectSnapshot:
        if not self._redo:
            raise SubtitleEditError("No subtitle edit to redo")
        next_snapshot = self._redo.pop()
        current = self.snapshot
        self._undo.append(current)
        # Redo follows the same rule: restore content while allocating a new
        # immutable project revision so concurrent clients never observe a
        # revision rollback.
        self.snapshot = replace(next_snapshot, revision=current.revision + 1)
        return self.snapshot
