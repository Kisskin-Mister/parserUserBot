from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any, Sequence

from browser_worker.evaluator import PageSnapshot
from browser_worker.security import URLGuard, URLValidationError

from .base import (
    BrowserRuntime,
    CaptchaPageMetadata,
    FormField,
    SubmitObservation,
)


class PlaywrightUnavailableError(RuntimeError):
    pass


class PlaywrightRuntime:
    """Optional real browser runtime; construct via ``PlaywrightRuntimeFactory``."""

    _SAFE_REF = re.compile(r"^bw-field-[0-9]+$")

    def __init__(self, *, context: Any, page: Any, session_key: str, guard: URLGuard):
        self.context = context
        self.page = page
        self.session_key = session_key
        self.guard = guard
        self._submit_attempted = False
        self.closed = False

    async def navigate(self, url: str) -> None:
        validated = await self.guard.validate(url)
        await self.page.goto(validated.url, wait_until="domcontentloaded")
        # Validate the final target as well as every intercepted redirect request.
        await self.guard.validate(self.page.url)

    async def current_url(self) -> str:
        return self.page.url

    async def snapshot(self, *, max_text_chars: int) -> PageSnapshot:
        data = await self.page.evaluate(
            """maxChars => {
                const clean = value => (value || '').replace(/\s+/g, ' ').trim();
                const main = document.querySelector('main, [role="main"], article');
                const bodyText = clean(document.body ? document.body.innerText : '');
                const mainText = clean(main ? main.innerText : '');
                const signals = [];
                if (document.querySelector('form')) signals.push('form_present');
                if (document.querySelector('[disabled][type="submit"]')) {
                    signals.push('submit_disabled');
                }
                return {
                    title: clean(document.title).slice(0, 500),
                    mainText: mainText.slice(0, maxChars),
                    bodyText: bodyText.slice(0, maxChars),
                    signals,
                };
            }""",
            max_text_chars,
        )
        return PageSnapshot(
            url=self.page.url,
            title=data["title"],
            main_text=data["mainText"],
            body_text=data["bodyText"],
            business_signals=tuple(data["signals"]),
        )

    async def extract_form_fields(self) -> Sequence[FormField]:
        rows = await self.page.evaluate(
            """() => {
                const result = [];
                const nodes = document.querySelectorAll('form input, form select, form textarea');
                const clean = value => (value || '').replace(/\s+/g, ' ').trim().slice(0, 500);
                for (let index = 0; index < nodes.length && index < 100; index++) {
                    const node = nodes[index];
                    const ref = `bw-field-${index}`;
                    node.setAttribute('data-browser-worker-ref', ref);
                    let label = '';
                    if (node.labels && node.labels.length) {
                        label = Array.from(node.labels).map(item => item.innerText).join(' ');
                    }
                    if (!label) label = node.getAttribute('aria-label') || '';
                    if (!label) {
                        const parentLabel = node.closest('label');
                        label = parentLabel ? parentLabel.innerText : '';
                    }
                    if (!label) {
                        const labelledBy = node.getAttribute('aria-labelledby');
                        if (labelledBy) {
                            label = labelledBy.split(/\s+/).map(id => {
                                const item = document.getElementById(id);
                                return item ? item.innerText : '';
                            }).join(' ');
                        }
                    }
                    let inputType = 'text';
                    if (node.tagName === 'SELECT') inputType = 'select-one';
                    else if (node.tagName === 'TEXTAREA') inputType = 'textarea';
                    else inputType = node.getAttribute('type') || 'text';
                    const options = node.tagName === 'SELECT'
                        ? Array.from(node.options).slice(0, 100).map(item => clean(item.textContent || item.value))
                        : [];
                    result.push({
                        ref,
                        label: clean(label),
                        inputType: clean(inputType).toLowerCase(),
                        name: clean(node.getAttribute('name')),
                        placeholder: clean(node.getAttribute('placeholder')),
                        autocomplete: clean(node.getAttribute('autocomplete')),
                        required: Boolean(node.required || node.getAttribute('aria-required') === 'true'),
                        options,
                    });
                }
                return result;
            }"""
        )
        return tuple(
            FormField(
                ref=row["ref"],
                label=row["label"],
                input_type=row["inputType"],
                name=row["name"],
                placeholder=row["placeholder"],
                autocomplete=row["autocomplete"],
                required=row["required"],
                options=tuple(row["options"]),
            )
            for row in rows
        )

    async def captcha_metadata(
        self, *, selectors: Sequence[str]
    ) -> CaptchaPageMetadata:
        data = await self.page.evaluate(
            """selectors => {
                const frames = Array.from(document.querySelectorAll('iframe'))
                    .slice(0, 100).map(item => (item.getAttribute('src') || '').slice(0, 500));
                const classes = Array.from(document.querySelectorAll('[class*="captcha" i], '
                    + '[class*="recaptcha" i], [class*="h-captcha" i], [class*="challenge" i]'))
                    .slice(0, 100).flatMap(item => Array.from(item.classList).map(value => value.slice(0, 100)));
                const matched = [];
                for (const selector of selectors.slice(0, 50)) {
                    try {
                        const item = document.querySelector(selector);
                        if (item && item.getClientRects().length) matched.push(selector);
                    } catch (_) { /* invalid configurable selector is simply ignored */ }
                }
                const visibleText = (document.body ? document.body.innerText : '')
                    .replace(/\s+/g, ' ').trim().slice(0, 5000);
                return {frames, classes, visibleText, matched};
            }""",
            list(selectors),
        )
        return CaptchaPageMetadata(
            iframe_sources=tuple(data["frames"]),
            class_names=tuple(data["classes"]),
            visible_text=data["visibleText"],
            matched_selectors=tuple(data["matched"]),
        )

    def _locator(self, field_ref: str) -> Any:
        if not self._SAFE_REF.fullmatch(field_ref):
            raise ValueError("invalid opaque form field ref")
        return self.page.locator(f'[data-browser-worker-ref="{field_ref}"]')

    async def fill_text(self, field_ref: str, value: str) -> None:
        await self._locator(field_ref).fill(value)

    async def select_option(self, field_ref: str, value: str) -> None:
        locator = self._locator(field_ref)
        try:
            await locator.select_option(label=value)
        except Exception:
            await locator.select_option(value=value)

    async def set_checked(self, field_ref: str, checked: bool) -> None:
        await self._locator(field_ref).set_checked(checked)

    async def upload_file(self, field_ref: str, path: Path) -> None:
        await self._locator(field_ref).set_input_files(str(path))

    async def read_form_values(self) -> dict[str, object]:
        rows = await self.page.evaluate(
            """() => Array.from(document.querySelectorAll('[data-browser-worker-ref]')).map(node => {
                const ref = node.getAttribute('data-browser-worker-ref');
                const type = (node.getAttribute('type') || '').toLowerCase();
                if (type === 'password') return [ref, null];
                if (type === 'checkbox' || type === 'radio') return [ref, Boolean(node.checked)];
                if (type === 'file') {
                    return [ref, node.files && node.files.length ? node.files[0].name : null];
                }
                return [ref, node.value];
            })"""
        )
        return dict(rows)

    async def screenshot(self, path: Path) -> None:
        await self.page.screenshot(path=str(path), full_page=True)

    async def submit_once(self) -> SubmitObservation:
        if self._submit_attempted:
            raise RuntimeError("submit has already been attempted in this session")
        self._submit_attempted = True
        candidates = self.page.locator(
            "form button[type='submit'], form input[type='submit'], "
            "form button:not([type])"
        )
        count = await candidates.count()
        button = None
        for index in range(count):
            item = candidates.nth(index)
            if await item.is_visible() and await item.is_enabled():
                button = item
                break
        if button is None:
            raise RuntimeError("no visible enabled submit control found")

        await button.click()
        try:
            await self.page.wait_for_load_state("domcontentloaded", timeout=10_000)
        except Exception:
            # The page may update in place without a navigation.  The click has
            # happened, so we inspect once and never retry it.
            pass
        observation = await self.page.evaluate(
            """() => {
                const text = (document.body ? document.body.innerText : '')
                    .replace(/\s+/g, ' ').trim();
                const patterns = [
                    /thank you.{0,200}/i,
                    /application (?:was )?(?:submitted|received).{0,200}/i,
                    /successfully applied.{0,200}/i,
                    /отклик (?:успешно )?отправлен.{0,200}/i,
                    /заявк[аи] (?:успешно )?(?:отправлена|принята).{0,200}/i,
                ];
                let confirmation = '';
                for (const pattern of patterns) {
                    const match = text.match(pattern);
                    if (match) { confirmation = match[0].slice(0, 500); break; }
                }
                const successNode = document.querySelector(
                    '[data-application-id], [data-testid*="success" i], '
                    + '[class*="application-success" i], [class*="submission-success" i]'
                );
                if (!confirmation && successNode) {
                    confirmation = (successNode.innerText || '').replace(/\s+/g, ' ').trim().slice(0, 500);
                }
                const providerId = successNode
                    ? successNode.getAttribute('data-application-id') : null;
                return {confirmation, providerId};
            }"""
        )
        return SubmitObservation(
            clicked=True,
            business_confirmed=bool(
                observation["confirmation"] or observation["providerId"]
            ),
            confirmation_text=observation["confirmation"] or None,
            confirmation_url=self.page.url,
            provider_application_id=observation["providerId"] or None,
        )

    async def close(self, *, preserve_session: bool) -> None:
        # Persistent context storage is retained for either value.  ``preserve``
        # communicates intent to injected runtimes; this implementation never
        # deletes its user-data directory.
        if not self.closed:
            await self.context.close()
            self.closed = True


class PlaywrightRuntimeFactory:
    """Creates isolated persistent Chromium contexts with lazy imports."""

    def __init__(
        self,
        *,
        user_data_root: Path,
        guard: URLGuard,
        headless: bool = True,
    ):
        self.user_data_root = user_data_root.resolve()
        self.guard = guard
        self.headless = headless
        self._playwright = None
        self._runtimes: dict[str, PlaywrightRuntime] = {}

    async def _ensure_playwright(self) -> Any:
        if self._playwright is None:
            try:
                from playwright.async_api import async_playwright
            except ImportError as exc:
                raise PlaywrightUnavailableError(
                    "Playwright is optional; install requirements-browser.txt "
                    "and run `playwright install chromium`"
                ) from exc
            self._playwright = await async_playwright().start()
        return self._playwright

    async def create(self, *, session_key: str) -> BrowserRuntime:
        existing = self._runtimes.get(session_key)
        if existing is not None and not existing.closed:
            return existing
        playwright = await self._ensure_playwright()
        self.user_data_root.mkdir(parents=True, exist_ok=True)
        profile_name = hashlib.sha256(session_key.encode("utf-8")).hexdigest()
        profile_path = self.user_data_root / profile_name
        context = await playwright.chromium.launch_persistent_context(
            str(profile_path),
            headless=self.headless,
            service_workers="block",
            accept_downloads=False,
        )

        async def guard_request(route: Any, request: Any) -> None:
            try:
                await self.guard.validate(
                    request.url, enforce_domain_allowlist=False
                )
            except URLValidationError:
                await route.abort("blockedbyclient")
            else:
                await route.continue_()

        await context.route("**/*", guard_request)
        page = context.pages[0] if context.pages else await context.new_page()
        runtime = PlaywrightRuntime(
            context=context,
            page=page,
            session_key=session_key,
            guard=self.guard,
        )
        self._runtimes[session_key] = runtime
        return runtime

    async def shutdown(self) -> None:
        for runtime in tuple(self._runtimes.values()):
            await runtime.close(preserve_session=True)
        self._runtimes.clear()
        if self._playwright is not None:
            await self._playwright.stop()
            self._playwright = None
