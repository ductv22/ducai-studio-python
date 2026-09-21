from __future__ import annotations

import json
import os
import re
import time
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path
from typing import Any, Protocol

from ducai_studio.providers.contracts import (
    HealthState,
    ProviderCapabilities,
    ProviderHealth,
    ProviderKind,
)

from .contracts import (
    AuthenticationRequired,
    ChallengeRequired,
    ProviderResponse,
    QuotaExhausted,
    TransientProviderFailure,
    TranslationRequest,
)

APP_URL = "https://gemini.google.com/app"
COMPOSER_SELECTORS = (
    "rich-textarea [contenteditable='true']",
    "[contenteditable='true'][role='textbox']",
    "textarea[placeholder]",
    "textarea",
)
RESPONSE_SELECTORS = (
    "model-response message-content",
    "model-response .model-response-text",
    "[data-message-author-role='model']",
    "[data-test-id='model-response']",
    "model-response",
)
STOP_SELECTORS = (
    "button[aria-label*='Stop response' i]",
    "button[aria-label*='Stop generating' i]",
    "button[aria-label*='Dừng phản hồi' i]",
    "button[aria-label*='Dừng tạo' i]",
    "button[data-test-id*='stop' i]",
)
SEND_SELECTORS = (
    "button[aria-label*='Send message' i]",
    "button[aria-label*='Gửi tin nhắn' i]",
    "button[aria-label*='Send' i]",
    "button[aria-label*='Gửi' i]",
)
TEMPORARY_CHAT_SELECTORS = (
    "button[aria-label*='Temporary chat' i]",
    "button[aria-label*='Trò chuyện tạm thời' i]",
    "button[aria-label*='Cuộc trò chuyện tạm thời' i]",
    "button[aria-label*='tạm thời' i]",
    "button:has-text('Temporary chat')",
    "button:has-text('Trò chuyện tạm thời')",
)
NEW_CHAT_SELECTORS = (
    "button[aria-label*='New chat' i]",
    "button[aria-label*='Cuộc trò chuyện mới' i]",
    "button[aria-label*='Trò chuyện mới' i]",
    "a[aria-label*='New chat' i]",
    "button:has-text('New chat')",
    "button:has-text('Cuộc trò chuyện mới')",
)
SIGN_IN_SELECTORS = (
    "a[href*='accounts.google.com']",
    "a[href*='/signin']",
    "a:has-text('Sign in')",
    "a:has-text('Đăng nhập')",
)
ERROR_SURFACE_SELECTORS = (
    "[role='alert']",
    "mat-snack-bar-container",
    ".mat-mdc-snack-bar-container",
    "[data-test-id*='error' i]",
    "[class*='error-message' i]",
)
TRANSIENT_TOKENS = (
    "something went wrong",
    "đã xảy ra lỗi",
    "try again later",
    "hãy thử lại sau",
    "temporarily unavailable",
    "tạm thời không khả dụng",
    "network error",
    "lỗi mạng",
)
TRANSIENT_NUMERIC_CODES = ("1095", "1905")
QUOTA_TOKENS = (
    "you've reached your limit",
    "you have reached your limit",
    "đã đạt đến giới hạn",
    "rate limit",
    "too many requests",
    "quota exceeded",
    "resource exhausted",
    "hết hạn mức",
)
CHALLENGE_TOKENS = (
    "captcha",
    "recaptcha",
    "unusual traffic",
    "verify it's you",
    "xác minh đó là bạn",
    "hoạt động bất thường",
    "không phải robot",
)


def _normalized(value: object) -> str:
    return " ".join(str(value or "").casefold().split())


def _json_complete(value: str) -> bool:
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", value.strip(), flags=re.IGNORECASE)
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        return False
    try:
        return isinstance(json.loads(text[start : end + 1]), dict)
    except json.JSONDecodeError:
        return False


def _first_response_timed_out(
    *,
    elapsed_seconds: float,
    saw_new_response: bool,
    first_response_timeout_seconds: float,
) -> bool:
    return not saw_new_response and elapsed_seconds >= first_response_timeout_seconds


def _response_stalled(
    *,
    stable_seconds: float,
    saw_new_response: bool,
    response_is_complete: bool,
    response_stall_timeout_seconds: float,
) -> bool:
    return (
        saw_new_response
        and not response_is_complete
        and stable_seconds >= response_stall_timeout_seconds
    )


class WebNoResponseStarted(TransientProviderFailure):
    code = "web_no_response"


class WebResponseStalled(TransientProviderFailure):
    code = "web_response_stalled"


def _browser_executable(preferred: str = "") -> Path | None:
    candidates: list[Path] = []
    if preferred:
        candidates.append(Path(preferred).expanduser())
    for env_name in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
        root = os.environ.get(env_name)
        if not root:
            continue
        candidates.extend(
            (
                Path(root) / "Google" / "Chrome" / "Application" / "chrome.exe",
                Path(root) / "Microsoft" / "Edge" / "Application" / "msedge.exe",
            )
        )
    return next((item.resolve() for item in candidates if item.is_file()), None)


class WebSession(Protocol):
    def submit(self, prompt: str, *, timeout_seconds: float) -> str: ...

    def close(self) -> None: ...


def _first_visible(
    page: Any,
    selectors: tuple[str, ...],
    *,
    timeout_ms: int = 0,
    interrupt_check: Callable[[], None] | None = None,
):
    deadline = time.monotonic() + max(0.0, timeout_ms / 1000)
    while True:
        if interrupt_check is not None:
            interrupt_check()
        for selector in selectors:
            try:
                locator = page.locator(selector)
                for index in range(locator.count() - 1, -1, -1):
                    item = locator.nth(index)
                    if item.is_visible():
                        return item
            except Exception:
                continue
        if time.monotonic() >= deadline:
            return None
        page.wait_for_timeout(250)


def _page_text(page: Any) -> str:
    try:
        return str(page.locator("body").inner_text(timeout=2_500) or "")
    except Exception:
        return ""


def _challenge_reason(page: Any, *, guest: bool) -> str:
    url = _normalized(getattr(page, "url", ""))
    text = _normalized(_page_text(page))
    if "accounts.google.com" in url:
        return "auth"
    if not guest and _first_visible(page, SIGN_IN_SELECTORS) is not None:
        return "auth"
    if not guest and any(token in text[:3_000] for token in ("sign in", "đăng nhập")):
        return "auth"
    if any(token in text for token in CHALLENGE_TOKENS):
        return "challenge"
    return ""


def _error_surfaces(page: Any) -> tuple[tuple[str, str], ...]:
    found: list[tuple[str, str]] = []
    seen: set[str] = set()
    for selector in ERROR_SURFACE_SELECTORS:
        try:
            locator = page.locator(selector)
            for index in range(locator.count()):
                item = locator.nth(index)
                if not item.is_visible():
                    continue
                text = _normalized(item.inner_text(timeout=1_000))
                kind = _error_kind(text, verified_surface=True)
                signature = f"{kind}|{text}"
                if kind and signature not in seen:
                    seen.add(signature)
                    found.append((kind, text))
        except Exception:
            continue
    if (
        not found
        and _first_visible(page, COMPOSER_SELECTORS) is None
        and _first_visible(page, RESPONSE_SELECTORS) is None
    ):
        text = _normalized(_page_text(page))
        kind = _error_kind(text, verified_surface=False)
        if kind:
            found.append((kind, "page-error-context"))
    return tuple(found)


def _error_kind(text: str, *, verified_surface: bool) -> str:
    normalized = _normalized(text)
    if any(token in normalized for token in QUOTA_TOKENS):
        return "quota"
    if any(token in normalized for token in TRANSIENT_TOKENS):
        return "transient"
    for code in TRANSIENT_NUMERIC_CODES:
        if verified_surface and re.search(
            rf"(?:^|\b)(?:error|code|lỗi|mã)\D{{0,20}}{code}(?:\b|$)",
            normalized,
        ):
            return "transient"
        if verified_surface and normalized == code:
            return "transient"
        if not verified_surface and re.search(
            rf"(?:error|code|lỗi|mã)\D{{0,20}}{code}(?:\b|$)", normalized
        ):
            return "transient"
    return ""


def _response_text(page: Any) -> str:
    for selector in RESPONSE_SELECTORS:
        try:
            locator = page.locator(selector)
            for index in range(locator.count() - 1, -1, -1):
                item = locator.nth(index)
                if item.is_visible():
                    value = str(item.inner_text(timeout=2_500) or "").strip()
                    if value:
                        return value
        except Exception:
            continue
    return ""


def _response_snapshot(page: Any) -> tuple[int, str]:
    """Return a response identity without retaining prompt or page-body text."""
    best_count = 0
    best_text = ""
    for selector in RESPONSE_SELECTORS:
        try:
            locator = page.locator(selector)
            visible: list[str] = []
            for index in range(locator.count()):
                item = locator.nth(index)
                if not item.is_visible():
                    continue
                value = str(item.inner_text(timeout=2_500) or "").strip()
                if value:
                    visible.append(value)
            if len(visible) > best_count:
                best_count = len(visible)
                best_text = visible[-1] if visible else ""
        except Exception:
            continue
    return best_count, best_text


def _page_dead_reason(page: Any) -> str:
    url = _normalized(getattr(page, "url", ""))
    if not url or url == "about:blank":
        return "blank_page"
    if _first_visible(page, COMPOSER_SELECTORS) is not None:
        return ""
    if _first_visible(page, RESPONSE_SELECTORS) is not None:
        return ""
    body = _normalized(_page_text(page))
    if len(body) < 20:
        return "empty_page"
    return ""


class PlaywrightGeminiSession:
    def __init__(
        self,
        *,
        profile_path: Path,
        headed: bool,
        guest: bool,
        temporary_chat: bool,
        browser_executable: str = "",
        first_response_timeout_seconds: float = 30.0,
        response_stall_timeout_seconds: float = 45.0,
        interrupt_check: Callable[[], None] | None = None,
    ) -> None:
        executable = _browser_executable(browser_executable)
        if executable is None:
            raise FileNotFoundError("Không tìm thấy Chrome hoặc Edge để chạy Gemini Web")
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise FileNotFoundError("Thiếu Playwright cho Gemini Web") from exc
        self._playwright = sync_playwright().start()
        self._browser = None
        self.guest = guest
        self.headed = headed
        self.temporary_chat = temporary_chat
        self.first_response_timeout_seconds = max(10.0, first_response_timeout_seconds)
        self.response_stall_timeout_seconds = max(15.0, response_stall_timeout_seconds)
        self.interrupt_check = interrupt_check or (lambda: None)
        options = {
            "executable_path": str(executable),
            "headless": not headed,
            "args": ("--no-default-browser-check", "--no-first-run"),
        }
        context_options = {"locale": "vi-VN", "viewport": {"width": 1360, "height": 900}}
        try:
            if guest:
                self._browser = self._playwright.chromium.launch(**options)
                self.context = self._browser.new_context(**context_options)
            else:
                profile_path.mkdir(parents=True, exist_ok=True)
                self.context = self._playwright.chromium.launch_persistent_context(
                    user_data_dir=str(profile_path),
                    **options,
                    **context_options,
                )
        except Exception:
            self._playwright.stop()
            raise
        self.page = self.context.pages[0] if self.context.pages else self.context.new_page()

    def close(self) -> None:
        with suppress(Exception):
            self.context.close()
        if self._browser is not None:
            with suppress(Exception):
                self._browser.close()
        with suppress(Exception):
            self._playwright.stop()

    def _open_home(self):
        last_error: Exception | None = None
        for attempt in range(2):
            try:
                self.interrupt_check()
                if self.page.is_closed():
                    self.page = self.context.new_page()
                self.page.goto(APP_URL, wait_until="domcontentloaded", timeout=90_000)
                reason = _challenge_reason(self.page, guest=self.guest)
                if reason in {"auth", "challenge"} and self.headed:
                    composer = self._wait_for_manual_access(timeout_seconds=900)
                    if composer is not None:
                        return composer
                if reason == "auth":
                    raise AuthenticationRequired(
                        "Phiên Gemini Web yêu cầu xác thực và không thể hoàn tất ở "
                        "chế độ ẩn. Hãy thử lại từ cache hoặc chọn Gemini API/CLI."
                    )
                if reason == "challenge":
                    raise ChallengeRequired(
                        "Gemini Web yêu cầu CAPTCHA/xác minh thủ công và không thể "
                        "hoàn tất ở chế độ ẩn. Hãy thử lại sau hoặc chọn Gemini API/CLI."
                    )
                composer = _first_visible(
                    self.page,
                    COMPOSER_SELECTORS,
                    timeout_ms=30_000,
                    interrupt_check=self.interrupt_check,
                )
                if composer is not None:
                    return composer
            except (AuthenticationRequired, ChallengeRequired):
                raise
            except Exception as exc:
                last_error = exc
            if attempt == 0:
                failed_page = self.page
                self.page = self.context.new_page()
                try:
                    if not failed_page.is_closed():
                        failed_page.close()
                except Exception:
                    pass
        raise TransientProviderFailure(
            "Gemini Web trắng/about:blank hoặc không có ô nhập sau khi thay tab."
        ) from last_error

    def _wait_for_manual_access(self, *, timeout_seconds: float):
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            self.interrupt_check()
            reason = _challenge_reason(self.page, guest=self.guest)
            composer = _first_visible(
                self.page,
                COMPOSER_SELECTORS,
                interrupt_check=self.interrupt_check,
            )
            if not reason and composer is not None:
                return composer
            self.page.wait_for_timeout(1_000)
        return None

    def _start_temporary_chat(self) -> None:
        if self.guest or not self.temporary_chat:
            return
        button = _first_visible(
            self.page,
            TEMPORARY_CHAT_SELECTORS,
            timeout_ms=10_000,
            interrupt_check=self.interrupt_check,
        )
        if button is None:
            raise TransientProviderFailure(
                "Gemini không hiện nút Trò chuyện tạm thời; dừng trước khi tạo lịch sử mới."
            )
        button.click(timeout=10_000)
        self.page.wait_for_timeout(500)

    def _start_fresh_guest_chat(self) -> None:
        if not self.guest:
            return
        button = _first_visible(
            self.page,
            NEW_CHAT_SELECTORS,
            timeout_ms=4_000,
            interrupt_check=self.interrupt_check,
        )
        if button is None:
            # Anonymous /app navigation commonly starts a clean composer by
            # itself. If it did not, the response baseline check below will
            # force a bounded session replacement rather than mixing chats.
            return
        button.click(timeout=10_000)
        self.page.wait_for_timeout(350)

    def submit(self, prompt: str, *, timeout_seconds: float) -> str:
        composer = self._open_home()
        self._start_fresh_guest_chat()
        composer = _first_visible(
            self.page,
            COMPOSER_SELECTORS,
            timeout_ms=8_000,
            interrupt_check=self.interrupt_check,
        ) or composer
        self._start_temporary_chat()
        if not self.guest and self.temporary_chat:
            composer = _first_visible(
                self.page,
                COMPOSER_SELECTORS,
                timeout_ms=15_000,
                interrupt_check=self.interrupt_check,
            )
            if composer is None:
                raise TransientProviderFailure(
                    "Gemini đã mở chat tạm thời nhưng không hiện lại ô nhập."
                )
        initial_errors = _error_surfaces(self.page)
        baseline_errors = {f"{kind}|{text}" for kind, text in initial_errors}
        if any(kind == "quota" for kind, _text in initial_errors):
            raise QuotaExhausted("Gemini Web xác nhận hết hạn mức.", verified=True)
        baseline_response_count, baseline_response = _response_snapshot(self.page)
        try:
            self.interrupt_check()
            composer.click(timeout=5_000)
            composer.fill(prompt, timeout=30_000)
        except Exception as exc:
            composer = _first_visible(
                self.page,
                COMPOSER_SELECTORS,
                timeout_ms=12_000,
                interrupt_check=self.interrupt_check,
            )
            if composer is None:
                raise TransientProviderFailure(
                    "Ô nhập Gemini bị thay thế khi trang tải."
                ) from exc
            composer.click(timeout=5_000)
            composer.fill(prompt, timeout=30_000)
        send = _first_visible(
            self.page,
            SEND_SELECTORS,
            timeout_ms=4_000,
            interrupt_check=self.interrupt_check,
        )
        if send is not None:
            send.click(timeout=10_000)
        else:
            composer.press("Enter")
        started = time.monotonic()
        stable_text = ""
        stable_since = started
        saw_new_response = False
        while time.monotonic() - started < max(60.0, timeout_seconds):
            self.interrupt_check()
            elapsed = time.monotonic() - started
            response_count, current = _response_snapshot(self.page)
            current_is_new = bool(
                current
                and (
                    response_count > baseline_response_count
                    or current != baseline_response
                )
            )
            if current_is_new:
                saw_new_response = True
                if current != stable_text:
                    stable_text = current
                    stable_since = time.monotonic()
                else:
                    generating = _first_visible(
                        self.page,
                        STOP_SELECTORS,
                        interrupt_check=self.interrupt_check,
                    ) is not None
                    stable_for = time.monotonic() - stable_since
                    if _json_complete(current) and (
                        (not generating and stable_for >= 1.5)
                        or (generating and stable_for >= 45.0)
                    ):
                        return current
                    if _response_stalled(
                        stable_seconds=stable_for,
                        saw_new_response=saw_new_response,
                        response_is_complete=_json_complete(current),
                        response_stall_timeout_seconds=self.response_stall_timeout_seconds,
                    ):
                        raise WebResponseStalled(
                            "Gemini Web đã bắt đầu nhưng để JSON dở đứng im trong "
                            f"{self.response_stall_timeout_seconds:g} giây; sẽ tạo "
                            "phiên sạch và gửi lại đúng chunk."
                        )
            reason = _challenge_reason(self.page, guest=self.guest)
            if reason == "auth":
                raise AuthenticationRequired(
                    "Phiên Gemini Web đã hết. Hãy thử lại từ cache hoặc chọn "
                    "Gemini API/CLI."
                )
            if reason == "challenge":
                raise ChallengeRequired("Gemini Web yêu cầu CAPTCHA/xác minh thủ công.")
            if _first_response_timed_out(
                elapsed_seconds=elapsed,
                saw_new_response=saw_new_response,
                first_response_timeout_seconds=self.first_response_timeout_seconds,
            ):
                raise WebNoResponseStarted(
                    "Gemini Web không bắt đầu phản hồi trong "
                    f"{self.first_response_timeout_seconds:g} giây; sẽ tạo phiên/tab sạch "
                    "thay vì chờ treo."
                )
            for kind, text in _error_surfaces(self.page):
                signature = f"{kind}|{text}"
                if kind == "quota":
                    raise QuotaExhausted("Gemini Web xác nhận hết hạn mức.", verified=True)
                if signature not in baseline_errors or (elapsed >= 8 and not saw_new_response):
                    raise TransientProviderFailure(
                        "Gemini Web hiện bề mặt lỗi tạm thời đã xác minh; "
                        "sẽ tạo phiên sạch."
                    )
            dead_reason = _page_dead_reason(self.page)
            if dead_reason and elapsed >= 3:
                raise TransientProviderFailure(
                    f"Gemini Web mất trang ({dead_reason}); sẽ tạo phiên sạch."
                )
            self.page.wait_for_timeout(500)
        raise TransientProviderFailure(
            "Gemini Web không hoàn tất phản hồi trước timeout; không dùng dữ liệu dở."
        )


class GeminiWebProvider:
    provider_id = "gemini-web"
    kind = ProviderKind.TRANSLATION
    provider_version = "playwright-isolated-v5"

    def __init__(
        self,
        *,
        model_id: str,
        profile_path: str | Path,
        headed: bool = False,
        guest: bool = False,
        temporary_chat: bool = True,
        browser_executable: str = "",
        timeout_seconds: float = 900.0,
        first_response_timeout_seconds: float = 30.0,
        response_stall_timeout_seconds: float = 45.0,
        session_retries: int = 1,
        max_requests_per_session: int = 5,
        session_factory: Callable[[], WebSession] | None = None,
        interrupt_check: Callable[[], None] | None = None,
    ) -> None:
        if (
            not model_id.strip()
            or session_retries < 0
            or not 1 <= max_requests_per_session <= 20
        ):
            raise ValueError("Gemini Web model and retry count are invalid")
        self.model_id = model_id.strip()
        self.profile_path = Path(profile_path).expanduser().resolve()
        self.headed = headed
        self.guest = guest
        self.temporary_chat = temporary_chat
        self.browser_executable = browser_executable
        self.timeout_seconds = timeout_seconds
        self.first_response_timeout_seconds = first_response_timeout_seconds
        self.response_stall_timeout_seconds = response_stall_timeout_seconds
        self.session_retries = session_retries
        self.max_requests_per_session = max_requests_per_session
        self.interrupt_check = interrupt_check
        self._session_factory = session_factory or self._new_session
        self._session: WebSession | None = None
        self._session_number = 0
        self._requests_in_session = 0

    def _new_session(self) -> WebSession:
        return PlaywrightGeminiSession(
            profile_path=self.profile_path,
            headed=self.headed,
            guest=self.guest,
            temporary_chat=self.temporary_chat,
            browser_executable=self.browser_executable,
            first_response_timeout_seconds=self.first_response_timeout_seconds,
            response_stall_timeout_seconds=self.response_stall_timeout_seconds,
            interrupt_check=self.interrupt_check,
        )

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            {
                "unstable_third_party_ui": True,
                "isolated_profile": not self.guest,
                "guest": self.guest,
                "headed": self.headed,
                "temporary_chat": self.temporary_chat,
                "session_recovery": True,
                "fresh_chat_per_request": True,
                "max_requests_per_session": self.max_requests_per_session,
            }
        )

    def health(self) -> ProviderHealth:
        executable = _browser_executable(self.browser_executable)
        try:
            import playwright.sync_api  # noqa: F401

            playwright_ready = True
        except ImportError:
            playwright_ready = False
        ready = executable is not None and playwright_ready
        return ProviderHealth(
            self.provider_id,
            HealthState.READY if ready else HealthState.NEEDS_CONFIGURATION,
            "not-probed",
            (
                "Gemini Web có runtime Playwright và trình duyệt."
                if ready
                else "Gemini Web thiếu Playwright hoặc Chrome/Edge."
            ),
            {"browser": str(executable or ""), "guest": self.guest},
        )

    def close(self) -> None:
        session, self._session = self._session, None
        if session is not None:
            session.close()
        self._requests_in_session = 0

    def generate(self, request: TranslationRequest) -> ProviderResponse:
        last_error: TransientProviderFailure | None = None
        for _attempt in range(self.session_retries + 1):
            try:
                if self._session is None:
                    self._session = self._session_factory()
                    self._session_number += 1
                    self._requests_in_session = 0
                started = time.monotonic()
                text = self._session.submit(
                    request.prompt, timeout_seconds=self.timeout_seconds
                )
            except (AuthenticationRequired, ChallengeRequired, QuotaExhausted):
                raise
            except TransientProviderFailure as exc:
                last_error = exc
                self.close()
                continue
            except Exception as exc:
                last_error = TransientProviderFailure(
                    "Trình duyệt Gemini Web đóng hoặc mất target "
                    f"({type(exc).__name__}: {str(exc)[:160]}); sẽ tạo phiên sạch."
                )
                self.close()
                continue
            elapsed_ms = max(1, int((time.monotonic() - started) * 1_000))
            self._requests_in_session += 1
            request_in_session = self._requests_in_session
            session_number = self._session_number
            rotate = request_in_session >= self.max_requests_per_session
            usage = {
                "transport": "playwright",
                "guest": self.guest,
                "fresh_chat": True,
                "session_number": session_number,
                "request_in_session": request_in_session,
                "session_rotated": rotate,
                "elapsed_ms": elapsed_ms,
            }
            if rotate:
                self.close()
            return ProviderResponse(
                request.request_id,
                text,
                self.provider_id,
                self.model_id,
                usage,
            )
        assert last_error is not None
        raise last_error
