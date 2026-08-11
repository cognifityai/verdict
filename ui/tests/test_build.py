from ui import build


def test_tracked_dashboard_pages_match_local_asset_shells():
    for filename, (title, script) in build.PAGES.items():
        if filename == "VerdictUI.html":
            continue
        assert (build.HERE / filename).read_text() == build.page(title, script)


def test_landing_source_link_is_a_real_link():
    assert 'href="https://github.com/cognifityai/verdict"' in (
        build.HERE / "VerdictUI.jsx"
    ).read_text()


def test_pages_do_not_load_runtime_code_from_public_cdns():
    forbidden = ("cdn.tailwindcss.com", "cdnjs.cloudflare.com", "unpkg.com", "text/babel")
    for filename in ("landing.html", "dashboard.html"):
        html = (build.HERE / filename).read_text()
        assert all(value not in html for value in forbidden)
        assert 'type="module"' in html


def test_compiled_assets_exist():
    assets = build.HERE / "assets"
    for filename in ("landing.js", "dashboard.js", "all-in-one.js", "verdict.css"):
        assert (assets / filename).is_file()


def test_public_landing_bundle_excludes_dashboard_sample_data():
    landing = (build.HERE / "assets" / "landing.js").read_text()
    assert "sample-trace-001" not in landing
    assert "LATEST PIPELINE RUN" not in landing
