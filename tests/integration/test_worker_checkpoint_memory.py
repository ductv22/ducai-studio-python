"""Checkpoint integrity must not require an artifact-sized bytes allocation."""

from hashlib import sha256
from pathlib import Path

import pytest

from ducai_studio.application.translation_pipeline import TranslationLedger
from ducai_studio.domain.effects import ExportSettings, ExportSnapshot, RenderClip
from ducai_studio.workers.export_worker import ExportWorker
from ducai_studio.workers.ocr_worker import OcrWorker
from ducai_studio.workers.translation_worker import TranslationWorker
from ducai_studio.workers.tts_worker import TtsWorker


@pytest.mark.parametrize("worker_type", [OcrWorker, TranslationWorker, TtsWorker, ExportWorker])
@pytest.mark.parametrize("size", [0, 17, 1024 * 1024, 2 * 1024 * 1024 + 17])
def test_checkpoint_hash_reads_are_bounded_and_match_published_artifact(
    tmp_path, monkeypatch, worker_type, size,
):
    monkeypatch.setenv("DUCAI_ARTIFACT_STAGING_ROOT", str(tmp_path / "staging"))
    worker = worker_type("bounded-checkpoint")
    messages = []
    monkeypatch.setattr(worker, "send", lambda kind, payload: messages.append((kind, payload)))
    artifact = worker.job_staging / "input.partial"
    artifact.write_bytes(b"x" * size)
    if isinstance(worker, TranslationWorker):
        worker.ledger = TranslationLedger("fixture", "fixture", "fixture", ())
    if isinstance(worker, ExportWorker):
        worker.ledger = {"entries": {}, "padding": "x" * size}
        worker.snapshot = ExportSnapshot(1, "p", 1, 0, 1_000_000, 160, 90,
            RenderClip("v", "V1", "video", "unused.mp4", 0, 1_000_000, 0, 1_000_000),
            (), (), (), ExportSettings())
    original_open = Path.open
    reads = []

    class BoundedReader:
        def __init__(self, stream):
            self.stream = stream

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return self.stream.__exit__(*args)

        def read(self, count=-1):
            assert 0 < count <= 1024 * 1024, "Checkpoint hash attempted an unbounded read"
            reads.append(count)
            return self.stream.read(count)

    def bounded_open(path, mode="r", *args, **kwargs):
        stream = original_open(path, mode, *args, **kwargs)
        return BoundedReader(stream) if mode == "rb" else stream

    monkeypatch.setattr(Path, "open", bounded_open)
    if isinstance(worker, ExportWorker):
        worker._checkpoint(())
    else:
        worker._checkpoint(artifact_path=artifact)
    assert reads
    payload = next(payload for kind, payload in messages if kind == "checkpoint")
    with original_open(Path(payload["artifact_path"]), "rb") as stream:
        expected = sha256(stream.read()).hexdigest()
    assert payload["expected_sha256"] == expected
    assert payload["artifact_state"] == "technical"
    assert payload["schema_version"] == 1
