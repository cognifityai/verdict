from ui import build


def test_tracked_dashboard_pages_match_local_asset_shells():
    for filename, (title, script) in build.PAGES.items():
        if filename == "VerdictUI.html":
            continue
        assert (build.HERE / filename).read_text() == build.page(title, script)


def test_generated_pages_fingerprint_local_assets():
    for filename, (_title, script) in build.PAGES.items():
        if filename == "VerdictUI.html":
            continue
        html = (build.HERE / filename).read_text()
        assert f'assets/{script}?v=' in html
        assert 'assets/verdict.css?v=' in html


def test_cluster_ids_are_not_reformatted_as_rubric_dimensions():
    source = (build.HERE / "VerdictUI.jsx").read_text()
    assert "label: dimensionLabel(cluster.cluster_id)" not in source
    assert "label: cluster.cluster_id" in source


def test_overview_does_not_label_a_missing_effect_size_as_clear():
    source = (build.HERE / "VerdictUI.jsx").read_text()
    assert "leadSignal ? statValue(leadSignal.cliffsDelta) : \"clear\"" in source
    assert "leadSignal?.cliffsDelta ?? \"clear\"" not in source


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


def test_publish_workflow_cannot_build_before_full_test_workflow():
    repo = build.HERE.parent
    workflow = (repo / ".github" / "workflows" / "publish.yml").read_text()
    tests = (repo / ".github" / "workflows" / "test.yml").read_text()

    assert "uses: ./.github/workflows/test.yml" in workflow
    assert "needs: verify" in workflow
    assert "workflow_call:" in tests
    assert "run: ruff check packages scripts ui" in tests
