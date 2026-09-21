from __future__ import annotations

import pytest

from ducai_studio.providers.translation.contracts import (
    AuthenticationRequired,
    QuotaExhausted,
    TransientProviderFailure,
    TranslationPurpose,
    TranslationRequest,
)
from ducai_studio.providers.translation.gemini_web import (
    GeminiWebProvider,
    WebNoResponseStarted,
    WebResponseStalled,
    _challenge_reason,
    _error_kind,
    _first_response_timed_out,
    _json_complete,
    _response_stalled,
)


def _request() -> TranslationRequest:
    return TranslationRequest(
        "web-chunk-7",
        TranslationPurpose.TRANSLATE,
        "PROMPT-CUA-CHUNK-7",
        (71, 72),
    )


class FakeSession:
    def __init__(self, result: str | Exception) -> None:
        self.result = result
        self.prompts: list[str] = []
        self.closed = False

    def submit(self, prompt: str, *, timeout_seconds: float) -> str:
        self.prompts.append(prompt)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result

    def close(self) -> None:
        self.closed = True


def test_1095_recreates_session_and_resends_exact_same_chunk() -> None:
    sessions = [
        FakeSession(TransientProviderFailure("1095")),
        FakeSession('{"translations":[{"id":71,"text":"A"}]}'),
    ]
    created: list[FakeSession] = []

    def factory() -> FakeSession:
        session = sessions.pop(0)
        created.append(session)
        return session

    provider = GeminiWebProvider(
        model_id="web-selected",
        profile_path="C:/isolated-test-profile",
        session_retries=1,
        session_factory=factory,
    )

    response = provider.generate(_request())

    assert response.request_id == "web-chunk-7"
    assert created[0].closed is True
    assert created[0].prompts == created[1].prompts == ["PROMPT-CUA-CHUNK-7"]


def test_guest_session_rotates_after_five_real_requests() -> None:
    created: list[FakeSession] = []

    def factory() -> FakeSession:
        session = FakeSession('{"translations":[{"id":71,"text":"A"}]}')
        created.append(session)
        return session

    provider = GeminiWebProvider(
        model_id="web-selected",
        profile_path="C:/isolated-test-profile",
        max_requests_per_session=5,
        session_factory=factory,
    )

    responses = [provider.generate(_request()) for _index in range(6)]

    assert len(created) == 2
    assert created[0].prompts == ["PROMPT-CUA-CHUNK-7"] * 5
    assert created[0].closed is True
    assert created[1].prompts == ["PROMPT-CUA-CHUNK-7"]
    assert created[1].closed is False
    assert responses[4].usage["request_in_session"] == 5
    assert responses[4].usage["session_rotated"] is True
    assert responses[5].usage["session_number"] == 2
    assert responses[5].usage["request_in_session"] == 1


def test_raw_browser_failure_recreates_session_and_resends_exact_same_chunk() -> None:
    sessions = [
        FakeSession(RuntimeError("Target page has been closed")),
        FakeSession('{"translations":[{"id":71,"text":"A"}]}'),
    ]
    created: list[FakeSession] = []

    def factory() -> FakeSession:
        session = sessions.pop(0)
        created.append(session)
        return session

    provider = GeminiWebProvider(
        model_id="web-selected",
        profile_path="C:/isolated-test-profile",
        session_retries=1,
        session_factory=factory,
    )

    response = provider.generate(_request())

    assert response.request_id == "web-chunk-7"
    assert created[0].closed is True
    assert created[0].prompts == created[1].prompts == ["PROMPT-CUA-CHUNK-7"]


def test_white_about_blank_failure_is_bounded_and_does_not_loop() -> None:
    created: list[FakeSession] = []

    def factory() -> FakeSession:
        session = FakeSession(TransientProviderFailure("white about:blank"))
        created.append(session)
        return session

    provider = GeminiWebProvider(
        model_id="web-selected",
        profile_path="C:/isolated-test-profile",
        session_retries=2,
        session_factory=factory,
    )

    with pytest.raises(TransientProviderFailure, match="about:blank"):
        provider.generate(_request())

    assert len(created) == 3
    assert all(session.closed for session in created)


@pytest.mark.parametrize(
    "failure",
    (
        AuthenticationRequired("login"),
        QuotaExhausted("quota", verified=True),
    ),
)
def test_auth_and_quota_are_not_hidden_by_session_retry(failure: Exception) -> None:
    session = FakeSession(failure)
    provider = GeminiWebProvider(
        model_id="web-selected",
        profile_path="C:/isolated-test-profile",
        session_retries=4,
        session_factory=lambda: session,
    )

    with pytest.raises(type(failure)):
        provider.generate(_request())

    assert session.prompts == ["PROMPT-CUA-CHUNK-7"]


def test_stream_guard_never_accepts_partial_or_merely_balanced_non_json() -> None:
    assert _json_complete('{"translations": []}') is True
    assert _json_complete('```json\n{"translations": []}\n```') is True
    assert _json_complete('{"translations": [') is False
    assert _json_complete("Gemini đang xử lý...") is False


def test_cue_id_1095_is_not_a_page_error_but_verified_error_code_is() -> None:
    assert _error_kind('{"id":1095,"text":"Bản dịch"}', verified_surface=False) == ""
    assert _error_kind("error code 1095", verified_surface=True) == "transient"
    assert _error_kind("1905", verified_surface=False) == ""


def test_first_response_timeout_is_separate_from_full_generation_timeout() -> None:
    assert _first_response_timed_out(
        elapsed_seconds=30.0,
        saw_new_response=False,
        first_response_timeout_seconds=30.0,
    )
    assert not _first_response_timed_out(
        elapsed_seconds=300.0,
        saw_new_response=True,
        first_response_timeout_seconds=30.0,
    )


def test_worker_control_signal_is_not_swallowed_by_web_session_recovery() -> None:
    class StopSignal(BaseException):
        pass

    class InterruptingSession:
        def __init__(self) -> None:
            self.closed = False

        def submit(self, prompt: str, *, timeout_seconds: float) -> str:
            raise StopSignal

        def close(self) -> None:
            self.closed = True

    session = InterruptingSession()
    provider = GeminiWebProvider(
        model_id="web-selected",
        profile_path="C:/isolated-test-profile",
        session_retries=4,
        session_factory=lambda: session,
    )

    with pytest.raises(StopSignal):
        provider.generate(_request())

    assert session.closed is False


def test_no_response_failure_has_a_distinct_retryable_error_code() -> None:
    failure = WebNoResponseStarted("no response")

    assert failure.retryable is True
    assert failure.code == "web_no_response"


def test_incomplete_response_stall_has_a_distinct_retryable_error_code() -> None:
    assert _response_stalled(
        stable_seconds=45.0,
        saw_new_response=True,
        response_is_complete=False,
        response_stall_timeout_seconds=45.0,
    )
    assert not _response_stalled(
        stable_seconds=600.0,
        saw_new_response=True,
        response_is_complete=True,
        response_stall_timeout_seconds=45.0,
    )
    assert WebResponseStalled("stalled").code == "web_response_stalled"


def test_signed_out_landing_page_is_detected_before_temporary_chat_lookup() -> None:
    class FakeLocator:
        def __init__(self, visible: bool) -> None:
            self.visible = visible

        def count(self) -> int:
            return 1 if self.visible else 0

        def nth(self, index: int) -> FakeLocator:
            assert index == 0
            return self

        def is_visible(self) -> bool:
            return self.visible

        def inner_text(self, *, timeout: int) -> str:
            assert timeout == 2_500
            return "Làm quen với Gemini"

    class FakePage:
        url = "https://gemini.google.com/app"

        def locator(self, selector: str) -> FakeLocator:
            if selector == "body":
                return FakeLocator(True)
            return FakeLocator("accounts.google.com" in selector)

    page = FakePage()

    assert _challenge_reason(page, guest=False) == "auth"
    assert _challenge_reason(page, guest=True) == ""
