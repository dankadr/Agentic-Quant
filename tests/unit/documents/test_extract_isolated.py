from trading.documents.extract import extract

CFG = dict(max_pages=10, timeout_seconds=30)


def test_html_scripts_and_styles_are_removed_and_the_rest_is_text() -> None:
    hostile = (
        b"<html><head><style>p{color:red}</style></head><body>"
        b"<script>fetch('http://169.254.169.254/')</script>"
        b"<p>Revenue rose 4%.</p><iframe src='http://internal/'></iframe></body></html>"
    )
    result = extract(hostile, "text/html", **CFG)
    assert result.status == "EXTRACTED"
    (text,) = result.pages
    assert text == "Revenue rose 4%."
    assert "fetch" not in text and "color" not in text and "internal" not in text


def test_plain_text_is_one_page() -> None:
    result = extract(b"Guidance raised.", "text/plain", **CFG)
    assert (result.status, result.pages) == ("EXTRACTED", ("Guidance raised.",))


def test_a_corrupt_pdf_is_a_recorded_failure_not_an_exception() -> None:
    result = extract(b"%PDF-1.4 not really a pdf", "application/pdf", **CFG)
    assert result.status == "FAILED"
    assert result.error is not None and result.error.startswith("PARSE_FAILED")


def test_a_pdf_with_no_text_layer_requires_ocr() -> None:
    import io

    from pypdf import PdfWriter

    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    buffer = io.BytesIO()
    writer.write(buffer)
    result = extract(buffer.getvalue(), "application/pdf", **CFG)
    assert result.status == "OCR_REQUIRED"


def test_the_child_process_gets_an_empty_environment() -> None:
    """No inherited credentials: the parser cannot read secrets from its environment."""
    import os

    os.environ["TRADING_LLM_API_KEY_FILE"] = "/run/secrets/llm_api_key"
    try:
        result = extract(b"hello", "text/plain", **CFG)
    finally:
        del os.environ["TRADING_LLM_API_KEY_FILE"]
    assert result.pages == ("hello",)
