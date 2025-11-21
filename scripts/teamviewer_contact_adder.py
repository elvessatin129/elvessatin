#!/usr/bin/env python3
"""Batch-add TeamViewer contacts via the web console using Playwright automation."""

from __future__ import annotations

import argparse
import asyncio
import csv
import getpass
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence

from playwright.async_api import (  # type: ignore
    Locator,
    Page,
    Playwright,
    TimeoutError as PlaywrightTimeoutError,
    async_playwright,
)

DEFAULT_SELECTORS: Dict[str, Any] = {
    "login": {
        "email": [
            {"type": "css", "value": "input[type='email']"},
            {"type": "css", "value": "input[name='email']"},
            {"type": "label", "value": "Email"},
            {"type": "placeholder", "value": "Email"}
        ],
        "password": [
            {"type": "css", "value": "input[type='password']"},
            {"type": "label", "value": "Password"},
            {"type": "placeholder", "value": "Password"}
        ],
        "submit": [
            {"type": "css", "value": "button[type='submit']"},
            {"type": "role", "role": "button", "name": "Sign in"},
            {"type": "role", "role": "button", "name": "Sign In"}
        ]
    },
    "navigation": {
        "contacts_ready": [
            {"type": "css", "value": "[data-testid='contacts-list']"},
            {"type": "text", "value": "Contacts", "exact": False}
        ]
    },
    "contacts": {
        "open_add_modal": [
            {"type": "css", "value": "button[data-testid='add-contact-button']"},
            {"type": "role", "role": "button", "name": "Add contact"}
        ],
        "email_input": [
            {"type": "css", "value": "input[type='email']"},
            {"type": "css", "value": "input[name='email']"}
        ],
        "name_input": [
            {"type": "css", "value": "input[name='alias']"},
            {"type": "placeholder", "value": "Name"}
        ],
        "confirm_button": [
            {"type": "role", "role": "button", "name": "Add"},
            {"type": "css", "value": "button[data-testid='confirm-add-contact']"}
        ],
        "success_indicator": [
            {"type": "css", "value": "[data-testid='toast-success']"},
            {"type": "css", "value": ".ms-MessageBar--success"}
        ],
        "close_modal": [
            {"type": "css", "value": "button[aria-label='Close']"}
        ]
    }
}

EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


@dataclass
class ContactRequest:
    email: str
    name: Optional[str]

    @property
    def label(self) -> str:
        return f"{self.name} <{self.email}>" if self.name else self.email

    def resolved_name(self) -> str:
        if self.name:
            return self.name
        local_part = self.email.split("@", maxsplit=1)[0]
        return local_part.replace(".", " ").replace("_", " ").title()


class SelectorResolver:
    def __init__(self, page: Page, selectors: Dict[str, Any]):
        self.page = page
        self.selectors = selectors

    async def fill(self, key: str, value: str, *, timeout_ms: int) -> bool:
        locator = await self._wait_for_first(key, timeout_ms)
        if not locator:
            return False
        await locator.fill(value)
        return True

    async def click(self, key: str, *, timeout_ms: int) -> bool:
        locator = await self._wait_for_first(key, timeout_ms)
        if not locator:
            return False
        await locator.click()
        return True

    async def wait_for_visible(self, key: str, *, timeout_ms: int) -> bool:
        for spec in self._lookup(key):
            locator = self._create_locator(spec)
            if locator is None:
                continue
            try:
                await locator.first.wait_for(state="visible", timeout=timeout_ms)
                return True
            except PlaywrightTimeoutError:
                continue
        return False

    async def _wait_for_first(self, key: str, timeout_ms: int) -> Optional[Locator]:
        last_error: Optional[Exception] = None
        for spec in self._lookup(key):
            locator = self._create_locator(spec)
            if locator is None:
                continue
            try:
                await locator.first.wait_for(state="visible", timeout=timeout_ms)
                return locator.first
            except PlaywrightTimeoutError as exc:
                last_error = exc
                continue
        if last_error:
            return None
        return None

    def _lookup(self, dotted_key: str) -> Sequence[Dict[str, Any]]:
        node: Any = self.selectors
        for part in dotted_key.split("."):
            if not isinstance(node, dict) or part not in node:
                return []
            node = node[part]
        if isinstance(node, list):
            return node
        return []

    def _create_locator(self, spec: Dict[str, Any]) -> Optional[Locator]:
        locator_type = spec.get("type", "css")
        value = spec.get("value")
        if locator_type == "css" and value:
            return self.page.locator(value)
        if locator_type == "role":
            role = spec.get("role") or spec.get("value")
            if role:
                return self.page.get_by_role(role, name=spec.get("name"), exact=spec.get("exact"))
        if locator_type == "label" and value:
            return self.page.get_by_label(value, exact=spec.get("exact", False))
        if locator_type == "placeholder" and value:
            return self.page.get_by_placeholder(value, exact=spec.get("exact", False))
        if locator_type == "text" and value:
            return self.page.get_by_text(value, exact=spec.get("exact", False))
        if locator_type == "test_id" and value:
            return self.page.get_by_test_id(value)
        return None


def parse_contacts(path: Path) -> List[ContactRequest]:
    if not path.exists():
        raise FileNotFoundError(f"Contact file not found: {path}")
    contacts: List[ContactRequest] = []
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "," in line:
            name_part, email_part = [segment.strip() for segment in line.split(",", maxsplit=1)]
            email = email_part
            name = name_part or None
        else:
            name = None
            email = line
        if not EMAIL_PATTERN.match(email):
            raise ValueError(f"Invalid email format detected: {email}")
        contacts.append(ContactRequest(email=email, name=name))
    return contacts


def load_selector_overrides(path: Optional[Path]) -> Dict[str, Any]:
    if not path:
        return {}
    if not path.exists():
        raise FileNotFoundError(f"Selector override file not found: {path}")
    with path.open(encoding="utf-8") as handler:
        return json.load(handler)


def deep_merge(base: Any, override: Any) -> Any:
    if isinstance(override, dict):
        base_map = base if isinstance(base, dict) else {}
        merged: Dict[str, Any] = {}
        for key, value in {**base_map, **override}.items():
            base_value = base_map.get(key)
            override_value = override.get(key)
            if key in override:
                merged[key] = deep_merge(base_value, override_value)
            else:
                merged[key] = base_value
        return merged
    return override if override is not None else base


class TeamViewerAutomator:
    def __init__(self, page: Page, selectors: Dict[str, Any]):
        self.page = page
        self.resolver = SelectorResolver(page, selectors)

    async def login(self, username: str, password: str, login_url: str, wait_seconds: float) -> None:
        await self.page.goto(login_url, wait_until="domcontentloaded")
        if not await self.resolver.fill("login.email", username, timeout_ms=15000):
            raise RuntimeError("Could not locate the email field")
        if not await self.resolver.fill("login.password", password, timeout_ms=15000):
            raise RuntimeError("Could not locate the password field")
        if not await self.resolver.click("login.submit", timeout_ms=15000):
            raise RuntimeError("Could not submit the login form")
        if wait_seconds:
            await self.page.wait_for_timeout(wait_seconds * 1000)

    async def open_contacts(self, contacts_url: str, ready_timeout_s: float) -> None:
        await self.page.goto(contacts_url, wait_until="load")
        ready = await self.resolver.wait_for_visible(
            "navigation.contacts_ready", timeout_ms=int(ready_timeout_s * 1000)
        )
        if not ready:
            raise RuntimeError("Contacts view did not become ready in time. Adjust selectors or timeout.")

    async def add_contact(self, contact: ContactRequest, timeout_s: float) -> None:
        timeout_ms = int(timeout_s * 1000)
        if not await self.resolver.click("contacts.open_add_modal", timeout_ms=timeout_ms):
            raise RuntimeError("Unable to open the 'Add contact' dialog. Update selectors if the UI changed.")
        if not await self.resolver.fill("contacts.email_input", contact.email, timeout_ms=timeout_ms):
            raise RuntimeError("Unable to fill the contact email field")
        name_to_use = contact.resolved_name()
        await self.resolver.fill("contacts.name_input", name_to_use, timeout_ms=timeout_ms)
        if not await self.resolver.click("contacts.confirm_button", timeout_ms=timeout_ms):
            raise RuntimeError("Failed to submit the add-contact form")
        success = await self.resolver.wait_for_visible("contacts.success_indicator", timeout_ms=timeout_ms)
        if not success:
            raise RuntimeError("No success indicator detected after submitting the contact")
        await self.resolver.click("contacts.close_modal", timeout_ms=5000)


def ensure_parent_directory(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def write_success_log(path: Path, contacts: Iterable[ContactRequest]) -> None:
    ensure_parent_directory(path)
    with path.open("w", encoding="utf-8", newline="") as csvfile:
        writer = csv.writer(csvfile)
        writer.writerow(["name", "email"])
        for contact in contacts:
            writer.writerow([contact.resolved_name(), contact.email])


def write_failure_log(path: Path, rows: Iterable[Dict[str, str]]) -> None:
    ensure_parent_directory(path)
    fieldnames = ["email", "name", "error"]
    with path.open("w", encoding="utf-8", newline="") as csvfile:
        writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Batch add TeamViewer contacts from a text file")
    parser.add_argument("--username", required=True, help="TeamViewer account email address")
    parser.add_argument("--password", help="TeamViewer password. If omitted, you will be prompted.")
    parser.add_argument("--email-file", default="data/sample_contacts.txt", type=Path, help="Path to the txt file containing contacts")
    parser.add_argument("--success-log", default=Path("data/successful_contacts.csv"), type=Path, help="Where to write successfully added contacts")
    parser.add_argument("--failure-log", default=Path("data/failed_contacts.csv"), type=Path, help="Optional CSV to record failures")
    parser.add_argument("--selectors", type=Path, default=None, help="Path to a JSON file overriding selector defaults")
    parser.add_argument("--headless", action="store_true", help="Run the browser in headless mode")
    parser.add_argument("--slowmo", type=int, default=0, help="Delay (ms) between Playwright actions for debugging")
    parser.add_argument("--login-url", default="https://account.teamviewer.com/", help="Login page URL")
    parser.add_argument("--contacts-url", default="https://web.teamviewer.com/contacts", help="Contacts page URL after login")
    parser.add_argument("--post-login-wait", type=float, default=5.0, help="Seconds to wait after logging in before navigation")
    parser.add_argument("--contacts-ready-timeout", type=float, default=20.0, help="Seconds to wait for the contacts UI to become ready")
    parser.add_argument("--action-timeout", type=float, default=15.0, help="Seconds to wait for modals and form submission")
    return parser.parse_args()


def resolve_password(args: argparse.Namespace) -> str:
    if args.password:
        return args.password
    env_password = os.getenv("TEAMVIEWER_PASSWORD")
    if env_password:
        return env_password
    return getpass.getpass("TeamViewer password: ")


def load_selectors(args: argparse.Namespace) -> Dict[str, Any]:
    overrides = load_selector_overrides(args.selectors) if args.selectors else {}
    return deep_merge(DEFAULT_SELECTORS, overrides)


async def run(args: argparse.Namespace) -> None:
    contacts = parse_contacts(args.email_file)
    if not contacts:
        raise RuntimeError("No contacts found in the provided file")
    password = resolve_password(args)
    selectors = load_selectors(args)

    async with async_playwright() as playwright:
        browser = await launch_browser(playwright, headless=args.headless, slow_mo=args.slowmo)
        context = await browser.new_context()
        page = await context.new_page()
        automator = TeamViewerAutomator(page, selectors)

        await automator.login(args.username, password, args.login_url, args.post_login_wait)
        await automator.open_contacts(args.contacts_url, args.contacts_ready_timeout)

        successes: List[ContactRequest] = []
        failures: List[Dict[str, str]] = []
        for entry in contacts:
            try:
                await automator.add_contact(entry, args.action_timeout)
                successes.append(entry)
                print(f"[SUCCESS] {entry.label}")
            except Exception as exc:
                failures.append({
                    "email": entry.email,
                    "name": entry.name or "",
                    "error": str(exc)
                })
                print(f"[FAILED ] {entry.label} -> {exc}")

        if successes:
            write_success_log(args.success_log, successes)
            print(f"Saved {len(successes)} successful contacts to {args.success_log}")
        if failures and args.failure_log:
            write_failure_log(args.failure_log, failures)
            print(f"Saved {len(failures)} failures to {args.failure_log}")

        await context.close()
        await browser.close()


def ensure_playwright_installed() -> None:
    try:
        import playwright  # noqa: F401
    except ImportError as exc:  # pragma: no cover
        raise SystemExit("Playwright is not installed. Run 'pip install -r requirements.txt' first.") from exc


def launch_browser(playwright: Playwright, headless: bool, slow_mo: int) -> Any:
    return playwright.chromium.launch(headless=headless, slow_mo=slow_mo)


def main() -> None:
    args = parse_args()
    ensure_playwright_installed()
    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        print("Aborted by user")
    except Exception as exc:
        raise SystemExit(f"Error: {exc}") from exc


if __name__ == "__main__":
    main()
