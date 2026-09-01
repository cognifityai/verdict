from verdict.dashboard import launcher


def test_product_launcher_opens_browser_and_preserves_arguments(monkeypatch) -> None:
    captured = []
    monkeypatch.setattr(launcher.app, "main", lambda argv: captured.append(argv) or 0)

    assert launcher.main(["--port", "9000"]) == 0
    assert captured == [["--open-browser", "--port", "9000"]]
