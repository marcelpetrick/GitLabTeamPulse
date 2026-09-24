"""Critical browser flows (VISION 37.4) against real HTTP servers."""

from __future__ import annotations

import re

import pytest
from playwright.sync_api import Browser, Page, expect

from .conftest import LiveStack

pytestmark = pytest.mark.e2e

ALEX = 2


def select_alex(page: Page, stack: LiveStack) -> None:
    page.goto(f"{stack.url}/#/people")
    expect(page.locator(".people-row[data-user-id]")).to_have_count(32)
    page.fill("#people-filter", "alex")
    rows = page.locator(".people-row[data-user-id]")
    expect(rows.first).to_be_visible()
    assert 1 < rows.count() < 32
    page.check(f"#select-{ALEX}")
    expect(page.locator("#selected-count")).to_have_text("1 selected")


def test_filter_select_clear_keeps_selection(
    page: Page, stack: LiveStack, browser: Browser
) -> None:
    page.goto(stack.url)
    expect(page).to_have_url(re.compile(r"#/people$|/$"))
    select_alex(page, stack)
    page.fill("#people-filter", "")
    expect(page.locator(".people-row[data-user-id]")).to_have_count(32)
    expect(page.locator(f"#select-{ALEX}")).to_be_checked()
    expect(page.locator("#tab-people-count")).to_have_text("1")

    # The selection is global: a second, independent browser sees it too.
    other = browser.new_context().new_page()
    other.goto(f"{stack.url}/#/people")
    expect(other.locator(f"#select-{ALEX}")).to_be_checked()
    other.context.close()


def test_dashboard_refresh_theme_and_copyable_text(page: Page, stack: LiveStack) -> None:
    select_alex(page, stack)
    page.click("#tab-dashboard")
    card = page.locator(f'article.card[data-user-id="{ALEX}"]')
    expect(card.locator(".work-table")).to_be_visible()
    expect(card.locator(".activity-item:visible")).to_have_count(5)
    card.get_by_label("Recent activity").get_by_role(
        "button", name=re.compile(r"Show all \d+")
    ).click()
    assert card.locator(".activity-item:visible").count() > 5
    expect(card.locator(".chart svg")).to_have_count(2)
    expect(page.locator("#status-left")).to_contain_text("Last update")

    # Sorting happens locally without a reload.
    card.locator('select[data-role="work-sort"]').select_option("title_asc")
    titles = card.locator(".work-table .title").all_inner_texts()
    assert titles == sorted(titles, key=str.lower)

    # Manual refresh shows the refreshing state while cached data stays visible.
    stack.control("latency", seconds=0.4)
    page.click("#refresh-now")
    expect(page.locator("#refresh-now")).to_contain_text("Refreshing")
    expect(card.locator(".work-table")).to_be_visible()
    expect(page.locator("#status-left [data-crawler]")).to_have_attribute(
        "data-crawler", "refreshing"
    )
    expect(page.locator("#refresh-now")).to_contain_text("Refresh now", timeout=30000)
    expect(page.locator(".pill.fresh").first).to_be_visible()

    # Theme toggle persists for the browser session.
    html = page.locator("html")
    expect(html).to_have_attribute("data-theme", "light")
    page.click("#theme-toggle")
    expect(html).to_have_attribute("data-theme", "dark")
    page.reload()
    expect(page.locator("html")).to_have_attribute("data-theme", "dark")

    # Ordinary text selection works on data views.
    user_select = page.locator(".work-table .title").first.evaluate(
        "el => getComputedStyle(el).userSelect"
    )
    assert user_select != "none"


def test_new_activity_appears_without_reload(page: Page, stack: LiveStack) -> None:
    select_alex(page, stack)
    page.click("#tab-dashboard")
    card = page.locator(f'article.card[data-user-id="{ALEX}"]')
    expect(card.locator(".activity-item").first).to_be_visible()
    before = card.locator(".activity-item").first.inner_text()
    stack.control("activity", user_id=ALEX)
    page.wait_for_timeout(1100)  # respect the manual refresh throttle
    page.click("#refresh-now")
    expect(card.locator(".activity-item").first.locator(".rel")).to_have_text(
        re.compile("just now|seconds ago")
    )
    assert page.evaluate("performance.getEntriesByType('navigation').length") == 1
    assert before is not None


def test_outage_marks_stale_error_and_keeps_data(page: Page, stack: LiveStack) -> None:
    select_alex(page, stack)
    page.click("#tab-dashboard")
    card = page.locator(f'article.card[data-user-id="{ALEX}"]')
    expect(card.locator(".work-table")).to_be_visible()
    rows = card.locator(".work-table tbody tr").count()
    stack.control("outage", down="true")
    page.wait_for_timeout(1100)
    page.click("#refresh-now")
    banner = page.locator(".banner.err")
    expect(banner).to_be_visible(timeout=30000)
    expect(banner).to_contain_text("Showing cached data")
    expect(card.locator(".work-table tbody tr")).to_have_count(rows)
    expect(card.locator(".pill.error")).to_be_visible()
    expect(page.locator("#diagnostics-count")).to_be_visible()
    page.click("#status-problems")
    drawer = page.locator("#diagnostics")
    expect(drawer).to_be_visible()
    expect(drawer.locator(".diag-item").first).to_contain_text("Cached data is still shown")
    page.keyboard.press("Escape")
    expect(drawer).to_be_hidden()

    stack.control("outage", down="false")
    page.wait_for_timeout(1100)
    page.click("#refresh-now")
    expect(banner).to_be_hidden(timeout=30000)
    expect(page.locator("#status-problems")).to_contain_text("No problems")


def test_empty_dashboard_links_to_people(page: Page, stack: LiveStack) -> None:
    page.goto(f"{stack.url}/#/dashboard")
    empty = page.locator(".empty")
    expect(empty).to_contain_text("Nobody is monitored yet")
    empty.get_by_role("button", name="Choose people").click()
    expect(page).to_have_url(re.compile("#/people$"))
    page.keyboard.press("Escape")
    page.locator("body").press("/")
    expect(page.locator("#people-filter")).to_be_focused()


def test_people_page_load_does_not_fetch_the_dashboard(page: Page, stack: LiveStack) -> None:
    requested: list[str] = []
    page.on("request", lambda request: requested.append(request.url))
    page.goto(f"{stack.url}/#/people")
    expect(page.locator(".people-row[data-user-id]")).to_have_count(32)
    assert not any(url.endswith("/api/dashboard") for url in requested)
    assert any(url.endswith("/api/users") for url in requested)
