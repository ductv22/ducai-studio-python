"""Release-local CUDA throughput fixes for the pinned VieNeu 3.3.0 runtime.

The external VieNeu environment is shared by every DUCAI release, so production
optimizations are installed in the engine-host process instead of mutating that
environment on disk.  Every private-runtime dependency is validated before the
patch is installed; an unsupported or CPU runtime keeps its original methods.
"""

from __future__ import annotations

import math
from typing import Any

OPTIMIZATION_SCHEMA = "ducai-vieneu-v330-cuda-batch-v1"
EXPECTED_RUNTIME_VERSION = "3.3.0"
DEFAULT_REPETITION_WINDOW = 64
DEFAULT_DECODE_BATCH_SIZE = 16


def install_vieneu_v330_optimizations(
    vieneu_engine: Any,
    *,
    runtime_version: str,
) -> dict[str, object]:
    """Install process-local CUDA batch methods and return an auditable receipt."""

    receipt: dict[str, object] = {
        "schema": OPTIMIZATION_SCHEMA,
        "enabled": False,
        "runtime_version": runtime_version,
        "repetition_window": DEFAULT_REPETITION_WINDOW,
        "decode_batch_size": DEFAULT_DECODE_BATCH_SIZE,
    }
    if runtime_version != EXPECTED_RUNTIME_VERSION:
        receipt["reason"] = "runtime_version_not_supported"
        return receipt
    if getattr(vieneu_engine, "backend", "") != "pytorch":
        receipt["reason"] = "backend_not_pytorch"
        return receipt
    runtime_engine = getattr(vieneu_engine, "engine", None)
    device = getattr(runtime_engine, "device", None)
    if getattr(device, "type", "") != "cuda":
        receipt["reason"] = "device_not_cuda"
        return receipt

    try:
        from vieneu.v3_turbo_serve import engine as engine_module

        batch_engine = engine_module.V3TurboBatchEngine
        required = (
            "generate_batch",
            "_generate_codes_batch",
            "_prompt_embeds",
        )
        if any(not hasattr(batch_engine, name) for name in required):
            receipt["reason"] = "runtime_batch_contract_not_supported"
            return receipt
        if not all(
            hasattr(engine_module, name)
            for name in ("_min_expected_frames", "max_expected_frames")
        ):
            receipt["reason"] = "runtime_guard_contract_not_supported"
            return receipt
        if getattr(batch_engine, "_ducai_optimization_schema", "") != OPTIMIZATION_SCHEMA:
            batch_engine._ducai_original_generate_batch = batch_engine.generate_batch
            batch_engine._ducai_original_generate_codes_batch = (
                batch_engine._generate_codes_batch
            )
            batch_engine.generate_batch = _generate_batch_optimized
            batch_engine._generate_codes_batch = _generate_codes_batch_optimized
            batch_engine._ducai_optimization_schema = OPTIMIZATION_SCHEMA
    except (AttributeError, ImportError, TypeError) as exc:
        receipt["reason"] = f"installation_failed:{type(exc).__name__}"
        return receipt

    receipt.update(
        {
            "enabled": True,
            "reason": "cuda_tensor_history_and_batched_codec",
            "synchronization": "one_batch_check_per_frame",
            "decoder": "moss_batch_decode_with_oom_backoff",
        }
    )
    return receipt


def _sample_batched(
    logits,
    *,
    temperature: float,
    top_k: int,
    top_p: float,
    repetition_penalty: float,
    history_counts,
):
    import torch
    import torch.nn.functional as functional

    if not math.isclose(repetition_penalty, 1.0) and history_counts is not None:
        seen = history_counts > 0
        penalized = torch.where(
            logits < 0,
            logits * repetition_penalty,
            logits / repetition_penalty,
        )
        logits = torch.where(seen, penalized, logits)
    if temperature <= 0:
        return logits.argmax(dim=-1)
    logits = logits / max(temperature, 1e-6)
    if top_k and 0 < top_k < logits.shape[-1]:
        threshold = torch.topk(logits, top_k, dim=-1).values[..., -1:]
        logits = torch.where(
            logits < threshold,
            torch.full_like(logits, float("-inf")),
            logits,
        )
    if 0.0 < top_p < 1.0:
        sorted_logits, sorted_indices = torch.sort(logits, descending=True, dim=-1)
        probabilities = functional.softmax(sorted_logits, dim=-1)
        drop = probabilities.cumsum(dim=-1) > top_p
        drop[..., 1:] = drop[..., :-1].clone()
        drop[..., 0] = False
        sorted_logits = sorted_logits.masked_fill(drop, float("-inf"))
        logits = torch.full_like(logits, float("-inf")).scatter_(
            -1, sorted_indices, sorted_logits
        )
    probabilities = functional.softmax(logits, dim=-1)
    return torch.multinomial(probabilities, num_samples=1).squeeze(-1)


def _update_history(
    *,
    counts,
    ring,
    channel: int,
    frame_index: int,
    codes,
    window: int,
) -> None:
    import torch

    channel_counts = counts[:, channel, :]
    additions = torch.ones((codes.shape[0], 1), device=codes.device, dtype=counts.dtype)
    if window > 0:
        slot = frame_index % window
        if frame_index >= window:
            expired = ring[:, channel, slot]
            channel_counts.scatter_add_(1, expired.unsqueeze(1), -additions)
        ring[:, channel, slot] = codes
    channel_counts.scatter_add_(1, codes.unsqueeze(1), additions)


def _generate_frame(
    model,
    backbone_hidden,
    *,
    temperature: float,
    top_k: int,
    top_p: float,
    repetition_penalty: float,
    history_counts,
    history_ring,
    history_window: int,
    frame_index: int,
):
    import torch

    config = model.config
    channels = config.n_vq
    hidden_size = config.hidden_size
    decoder = model.acoustic_decoder
    layers = len(decoder.layers)
    dtype = next(decoder.parameters()).dtype
    device = backbone_hidden.device
    batch = backbone_hidden.shape[0]

    def sample_channel(channel: int, values):
        logits = model.audio_lm_heads[channel](values).float()
        codes = _sample_batched(
            logits,
            temperature=temperature,
            top_k=top_k,
            top_p=top_p,
            repetition_penalty=repetition_penalty,
            history_counts=(
                history_counts[:, channel, :] if history_counts is not None else None
            ),
        )
        if history_counts is not None:
            _update_history(
                counts=history_counts,
                ring=history_ring,
                channel=channel,
                frame_index=frame_index,
                codes=codes,
                window=history_window,
            )
        return codes

    condition = backbone_hidden.to(dtype)
    start_ids = torch.full(
        (batch,),
        config.speech_generation_start_token_id,
        device=device,
        dtype=torch.long,
    )
    text = model.text_embeddings(start_ids).to(dtype)
    tokens = torch.stack([condition, text], dim=1)
    positions = torch.arange(2, device=device, dtype=torch.long)
    hidden, past_keys, past_values = decoder.cached_step(
        tokens, positions, [None] * layers, [None] * layers
    )
    prefill_output = hidden

    generated = [sample_channel(0, hidden[:, 1])]
    for channel in range(1, channels):
        embedded = model.audio_embeddings[channel - 1](generated[-1]).to(dtype)
        positions = torch.arange(channel + 1, channel + 2, device=device, dtype=torch.long)
        hidden, past_keys, past_values = decoder.cached_step(
            embedded.view(batch, 1, hidden_size),
            positions,
            past_keys,
            past_values,
        )
        generated.append(sample_channel(channel, hidden[:, 0]))
    return torch.stack(generated, dim=1), prefill_output


def _generate_codes_batch_optimized(
    self,
    requests,
    *,
    temperature: float = 0.8,
    top_k: int = 25,
    top_p: float = 0.95,
    repetition_penalty: float = 1.2,
    repetition_window: int = DEFAULT_REPETITION_WINDOW,
    max_new_frames: int = 300,
    use_cudagraph: bool = False,
    frame_caps=None,
):
    """Preserve the runtime's ``@torch.no_grad`` contract after monkeypatching."""

    import torch

    with torch.no_grad():
        return _generate_codes_batch_optimized_impl(
            self,
            requests,
            temperature=temperature,
            top_k=top_k,
            top_p=top_p,
            repetition_penalty=repetition_penalty,
            repetition_window=repetition_window,
            max_new_frames=max_new_frames,
            use_cudagraph=use_cudagraph,
            frame_caps=frame_caps,
        )


def _generate_codes_batch_optimized_impl(
    self,
    requests,
    *,
    temperature: float = 0.8,
    top_k: int = 25,
    top_p: float = 0.95,
    repetition_penalty: float = 1.2,
    repetition_window: int = DEFAULT_REPETITION_WINDOW,
    max_new_frames: int = 300,
    use_cudagraph: bool = False,
    frame_caps=None,
):
    import torch

    del use_cudagraph  # Dynamic repetition tensors retain the quality-first policy.
    config = self.config
    device = self.tts.device
    if device.type != "cuda":
        return self._ducai_original_generate_codes_batch(
            requests,
            temperature=temperature,
            top_k=top_k,
            top_p=top_p,
            repetition_penalty=repetition_penalty,
            repetition_window=repetition_window,
            max_new_frames=max_new_frames,
            use_cudagraph=False,
            frame_caps=frame_caps,
        )

    embeddings = [self._prompt_embeds(request) for request in requests]
    batch = len(requests)
    speaker_embeddings = [
        self.tts._resolve_speaker_emb(request.get("speaker_emb")) for request in requests
    ]
    batch_speaker = (
        torch.cat(speaker_embeddings, dim=0)
        if speaker_embeddings and speaker_embeddings[0] is not None
        else None
    )

    use_repetition = not math.isclose(repetition_penalty, 1.0)
    history_window = int(repetition_window)
    history_counts = None
    history_ring = None
    if use_repetition:
        vocabulary_size = int(self.model.audio_lm_heads[0].out_features)
        history_counts = torch.zeros(
            (batch, config.n_vq, vocabulary_size),
            device=device,
            dtype=torch.int32,
        )
        if history_window > 0:
            history_ring = torch.full(
                (batch, config.n_vq, history_window),
                -1,
                device=device,
                dtype=torch.long,
            )

    hidden, cache, mask, position = self.bb.prefill(embeddings)
    finished = torch.zeros(batch, device=device, dtype=torch.bool)
    lengths = torch.zeros(batch, device=device, dtype=torch.long)
    cap_tensor = (
        torch.as_tensor(frame_caps, device=device, dtype=torch.long)
        if frame_caps is not None
        else None
    )
    generated_steps = []

    for frame_index in range(max_new_frames):
        codes, prefill_output = _generate_frame(
            self.model,
            hidden,
            temperature=temperature,
            top_k=top_k,
            top_p=top_p,
            repetition_penalty=repetition_penalty,
            history_counts=history_counts,
            history_ring=history_ring,
            history_window=history_window,
            frame_index=frame_index,
        )
        generated_steps.append(codes)
        active = ~finished
        lengths.add_(active.to(lengths.dtype))
        eos = (
            self.model.text_lm_head(prefill_output[:, 0]).float().argmax(-1)
            == config.speech_generation_end_token_id
        )
        if cap_tensor is not None:
            eos = eos | (lengths >= cap_tensor)
        finished |= active & eos
        if bool(finished.all().item()):
            break

        slot = torch.full(
            (batch, 1, config.n_vq + 1),
            config.audio_pad_token_id,
            dtype=torch.long,
            device=device,
        )
        slot[:, :, 0] = config.speech_generation_start_token_id
        slot[:, 0, 1:] = codes
        slot_embeddings = self.model._build_inputs_embeds(
            slot, speaker_emb=batch_speaker
        )
        hidden, cache, mask, position = self.bb.decode_step(
            slot_embeddings, cache, mask, position
        )

    if not generated_steps:
        return [
            torch.zeros((0, config.n_vq), dtype=torch.long, device=device)
            for _ in requests
        ]
    stacked = torch.stack(generated_steps, dim=0)
    materialized_lengths = lengths.cpu().tolist()
    return [
        stacked[: int(materialized_lengths[index]), index, :].clone()
        for index in range(batch)
    ]


def _decode_codes_batched(tts, codes, *, preferred_batch_size: int):
    import numpy as np
    import torch

    results = [np.zeros(0, dtype=np.float32) for _ in codes]
    ordered = sorted(
        (index for index, value in enumerate(codes) if len(value)),
        key=lambda index: len(codes[index]),
    )
    if not ordered:
        return results
    tokenizer = tts.audio_tokenizer
    batch_decode = getattr(tokenizer, "batch_decode", None)
    if not callable(batch_decode):
        for index in ordered:
            results[index] = tts._decode_codes(codes[index])
        return results

    decode_batch_size = max(1, min(int(preferred_batch_size), len(ordered)))
    cursor = 0
    while cursor < len(ordered):
        current = min(decode_batch_size, len(ordered) - cursor)
        indices = ordered[cursor : cursor + current]
        try:
            code_list = [codes[index].transpose(0, 1) for index in indices]
            with torch.no_grad():
                decoded = batch_decode(
                    code_list,
                    num_quantizers=tts.config.n_vq,
                    streaming=False,
                )
            audio = decoded.audio.float().cpu()
            lengths = decoded.audio_lengths.cpu().tolist()
            for row, index in enumerate(indices):
                duration = int(lengths[row])
                results[index] = (
                    audio[row, :, :duration].mean(0).numpy().astype(np.float32, copy=True)
                )
            cursor += current
        except (AttributeError, RuntimeError, TypeError, ValueError) as exc:
            if "out of memory" in str(exc).casefold() and current > 1:
                decode_batch_size = max(1, current // 2)
                torch.cuda.empty_cache()
                continue
            # The runtime decoder is a private API. Preserve production output if
            # its batched path rejects a shape/device or batch=1 still exceeds the
            # available memory: use the exact decoder that VieNeu 3.3 ships with.
            for index in indices:
                results[index] = tts._decode_codes(codes[index].cpu())
            cursor += current
    return results


def _generate_batch_optimized(
    self,
    requests,
    *,
    temperature: float = 0.8,
    top_k: int = 25,
    top_p: float = 0.95,
    repetition_penalty: float = 1.2,
    repetition_window: int = DEFAULT_REPETITION_WINDOW,
    max_new_frames: int = 300,
    use_cudagraph: bool = False,
    max_retries: int = 2,
    frame_cap: bool = True,
):
    """Preserve the runtime's ``@torch.no_grad`` contract after monkeypatching."""

    import torch

    with torch.no_grad():
        return _generate_batch_optimized_impl(
            self,
            requests,
            temperature=temperature,
            top_k=top_k,
            top_p=top_p,
            repetition_penalty=repetition_penalty,
            repetition_window=repetition_window,
            max_new_frames=max_new_frames,
            use_cudagraph=use_cudagraph,
            max_retries=max_retries,
            frame_cap=frame_cap,
        )


def _generate_batch_optimized_impl(
    self,
    requests,
    *,
    temperature: float = 0.8,
    top_k: int = 25,
    top_p: float = 0.95,
    repetition_penalty: float = 1.2,
    repetition_window: int = DEFAULT_REPETITION_WINDOW,
    max_new_frames: int = 300,
    use_cudagraph: bool = False,
    max_retries: int = 2,
    frame_cap: bool = True,
):
    import numpy as np
    from vieneu.v3_turbo_serve import engine as engine_module
    from vieneu_utils.phonemize_text import phonemize_text_with_emotions

    requests_with_phonemes = []
    for request in requests:
        if request.get("phonemes") is None:
            request = {
                **request,
                "phonemes": phonemize_text_with_emotions(request["text"]),
            }
        requests_with_phonemes.append(request)

    sampling = {
        "temperature": temperature,
        "top_k": top_k,
        "top_p": top_p,
        "repetition_penalty": repetition_penalty,
        "repetition_window": repetition_window,
        "max_new_frames": max_new_frames,
        "use_cudagraph": use_cudagraph,
    }
    caps = (
        [
            min(max_new_frames, engine_module.max_expected_frames(request["phonemes"]))
            for request in requests_with_phonemes
        ]
        if frame_cap
        else None
    )
    codes = self._generate_codes_batch(
        requests_with_phonemes, frame_caps=caps, **sampling
    )

    floors = [
        engine_module._min_expected_frames(request["phonemes"])
        for request in requests_with_phonemes
    ]
    for _attempt in range(max(0, max_retries)):
        invalid = [index for index in range(len(codes)) if len(codes[index]) < floors[index]]
        if not invalid:
            break
        retries = self._generate_codes_batch(
            [requests_with_phonemes[index] for index in invalid],
            frame_caps=[caps[index] for index in invalid] if caps is not None else None,
            **sampling,
        )
        for retry_index, original_index in enumerate(invalid):
            if len(retries[retry_index]) > len(codes[original_index]):
                codes[original_index] = retries[retry_index]

    if getattr(self.tts.device, "type", "") != "cuda":
        return [
            self.tts._decode_codes(code) if len(code) else np.zeros(0, dtype=np.float32)
            for code in codes
        ]
    return _decode_codes_batched(
        self.tts,
        codes,
        preferred_batch_size=DEFAULT_DECODE_BATCH_SIZE,
    )
