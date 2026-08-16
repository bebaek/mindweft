from app.thread_titles import generate_thread_title, normalize_manual_thread_title
from minigent_client.thread_titles import thread_title_from_message


def test_generate_thread_title_removes_request_boilerplate() -> None:
    message = "Could you please take a look at the token refresh code and see why it fails?"

    assert (
        generate_thread_title(message) == "Investigate the token refresh code and see why it fails"
    )
    assert thread_title_from_message(message) == generate_thread_title(message)


def test_generate_thread_title_normalizes_and_truncates() -> None:
    title = generate_thread_title("  Please   add tests for " + "the config parser " * 8)

    assert title.startswith("Add tests for the config parser")
    assert title.endswith("…")
    assert len(title) == 64


def test_normalize_manual_thread_title_preserves_meaningful_text() -> None:
    assert normalize_manual_thread_title("  Fix   token refresh race  ") == "Fix token refresh race"
