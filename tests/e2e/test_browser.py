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


def test_contribution_calendar_2d_and_3d(page: Page, stack: LiveStack) -> None:
    select_alex(page, stack)
    page.click("#tab-dashboard")
    card = page.locator(f'article.card[data-user-id="{ALEX}"]')
    section = card.locator("section.contributions")
    expect(section).to_contain_text("contributions")
    cells = section.locator("rect.heat-cell")
    expect(cells.first).to_be_visible()
    assert 52 * 7 < cells.count() <= 53 * 7
    busy = section.locator("rect.heat-cell:not(.lvl-0)")
    # Waits: the grid can render empty before the contribution backfill lands.
    expect(busy.nth(50)).to_be_attached()  # a year of history, not only the last week
    busy.first.hover()
    expect(page.locator("#tooltip")).to_contain_text("contribution")
    section.locator("svg.heatmap").focus()
    live = section.locator(".heatmap-live[aria-live=polite]")
    expect(live).to_contain_text("contribution")
    latest = live.text_content()
    page.keyboard.press("ArrowLeft")
    expect(section.locator("rect.heat-cell.is-active")).to_have_count(1)
    expect(live).not_to_have_text(latest or "")
    expect(live).to_contain_text("contribution")

    section.get_by_role("button", name="3D skyline").click()
    canvas = section.locator("canvas.skyline-canvas")
    expect(canvas).to_be_visible()
    page.wait_for_timeout(300)
    snapshot = "el => el.toDataURL()"
    canvas.scroll_into_view_if_needed()
    initial = canvas.evaluate(snapshot)
    box = canvas.bounding_box()
    assert box is not None
    page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
    page.mouse.down()
    page.mouse.move(box["x"] + box["width"] / 2 + 120, box["y"] + box["height"] / 2, steps=6)
    page.mouse.up()
    rotated = canvas.evaluate(snapshot)
    assert rotated != initial
    page.mouse.wheel(0, -300)
    page.wait_for_timeout(100)
    zoomed = canvas.evaluate(snapshot)
    assert zoomed != rotated
    canvas.focus()
    page.keyboard.press("ArrowRight")
    turned = canvas.evaluate(snapshot)
    assert turned != zoomed

    # The rotation and zoom survive the re-render a refresh triggers (a new canvas element).
    canvas.evaluate("el => { el.dataset.beforeRefresh = '1'; }")
    page.click("#refresh-now")
    expect(page.locator("#refresh-now")).to_contain_text("Refresh now", timeout=30000)
    expect(section.locator("canvas.skyline-canvas:not([data-before-refresh])")).to_be_visible()
    canvas = section.locator("canvas.skyline-canvas")
    page.wait_for_timeout(300)  # the first draw runs on requestAnimationFrame
    assert canvas.evaluate(snapshot) == turned

    section.get_by_role("button", name="Reset view").click()
    assert canvas.evaluate(snapshot) == initial


def test_attention_strip_milestones_and_estimates(page: Page, stack: LiveStack) -> None:
    select_alex(page, stack)
    page.click("#tab-dashboard")
    attention = page.locator("section.attention")
    expect(attention.locator(".attention-row").first).to_be_visible(timeout=30000)
    stalled = attention.locator('.attention-tile[data-code="stalled"]')
    expect(stalled.locator(".count")).not_to_have_text("0")
    stalled.click()
    expect(stalled).to_have_attribute("aria-pressed", "true")
    rows = attention.locator(".attention-row")
    for index in range(rows.count()):
        expect(rows.nth(index).locator('[data-reason="stalled"]')).to_contain_text("No update for")
    stalled.click()  # a second click shows everything again
    expect(attention.locator('.attention-tile[data-code="all"]')).to_have_attribute(
        "aria-pressed", "true"
    )
    milestones = page.locator("section.milestones")
    expect(milestones.locator(".milestone").first).to_be_visible(timeout=30000)
    expect(milestones.locator(".milestone-progress").first).to_contain_text("closed")

    card = page.locator(f'article.card[data-user-id="{ALEX}"]')
    expect(card.locator('[data-alert="stalled"]')).to_be_visible()
    expect(card.locator("tr.is-stalled").first).to_contain_text("no update for")
    card.get_by_role("button", name="No estimate").click()
    expect(card.locator(".work .panel-head h3")).to_contain_text("Work ·")
    rows = card.locator(".work-table tbody tr")
    if rows.count() == 0:
        expect(card.locator(".work .card-empty")).to_have_text("No items match this filter.")
    for index in range(rows.count()):  # "no estimate", or time spent without an estimate
        expect(rows.nth(index).locator(".time-chip")).to_have_text(re.compile(r"no estimate|/ —$"))


def test_create_team_see_report_and_show_it_on_the_dashboard(page: Page, stack: LiveStack) -> None:
    page.goto(f"{stack.url}/#/teams")
    expect(page.get_by_role("heading", name="No teams yet")).to_be_visible()
    page.click("#new-team")
    editor = page.locator("#team-editor")
    page.fill("#team-name", "Platform")
    expect(editor.locator(".member-option").first).to_be_visible()
    editor.get_by_placeholder("Filter people…").fill("alex.berger")
    editor.locator(f"#member-{ALEX}").check()
    editor.get_by_placeholder("Filter people…").fill("sam.kowalski")
    editor.locator(".member-option input").first.check()
    page.fill("#team-scope", "platform")
    editor.get_by_role("button", name="Add").click()
    expect(editor.locator(".scope-chip")).to_contain_text("platform")
    page.click("#save-team")
    expect(editor).to_be_hidden()
    expect(page.locator("#tab-teams-count")).to_have_text("1")
    expect(page.locator(".team-head h2")).to_have_text("Platform")
    # The history of both members loads in the background; then the quarter has data.
    page.get_by_role("button", name="Quarter").click()
    expect(page.locator(".pill.fresh", has_text="History")).to_be_visible(timeout=60000)
    expect(page.locator('.kpi[data-kpi="hours"] .kpi-value')).not_to_have_text("0m")
    expect(page.locator(".share-table tbody tr").first).to_contain_text("platform/")
    page.get_by_role("button", name="Previous period").click()
    expect(page.locator(".period-label")).to_contain_text("Q")
    page.locator(".counting summary").click()
    expect(page.locator(".counting dl")).to_contain_text("Knowledge spread")

    page.once("dialog", lambda dialog: dialog.accept())
    page.get_by_role("button", name="Show on dashboard").click()
    expect(page).to_have_url(re.compile(r"#/dashboard$"))
    expect(page.locator("article.card[data-user-id]")).to_have_count(2, timeout=30000)


def test_about_dialog(page: Page, stack: LiveStack) -> None:
    page.goto(stack.url)
    page.click("#open-about")
    dialog = page.locator("#about")
    expect(dialog).to_be_visible()
    expect(dialog.locator("#about-version")).to_have_text(re.compile(r"^v\d+\.\d+\.\d+$"))
    expect(dialog).to_contain_text("Marcel Petrick")
    expect(dialog).to_contain_text("GPL-3.0-or-later")
    expect(dialog.locator(".about-deps tbody tr", has_text="fastapi")).to_be_visible()
    page.keyboard.press("Escape")
    expect(dialog).to_be_hidden()
