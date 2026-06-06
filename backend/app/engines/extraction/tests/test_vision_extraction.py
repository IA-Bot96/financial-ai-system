"""Tests for vision-assisted reconstruction (page image + text -> GPT)."""
from app.engines.extraction.pipeline.gpt_tables import _VISION_NOTE, _render_page_png
from app.engines.extraction.services import prompts
from app.engines.extraction.services.gpt_client import _user_content


def test_user_content_text_only_is_plain_string():
    # No images -> unchanged behaviour: the content is the plain user string.
    assert _user_content("hello", None) == "hello"
    assert _user_content("hello", []) == "hello"


def test_user_content_with_images_builds_multimodal_blocks():
    content = _user_content("read this", [b"\x89PNG_fake", b"\x89PNG_two"], detail="high")
    assert isinstance(content, list)
    assert content[0] == {"type": "text", "text": "read this"}
    imgs = content[1:]
    assert len(imgs) == 2
    for blk in imgs:
        assert blk["type"] == "image_url"
        assert blk["image_url"]["url"].startswith("data:image/png;base64,")
        assert blk["image_url"]["detail"] == "high"


def test_render_page_png_produces_png_bytes(tmp_path):
    import fitz

    doc = fitz.open()
    doc.new_page().insert_text((72, 72), "Statement of Financial Position 2025")
    p = tmp_path / "x.pdf"
    doc.save(p)
    doc.close()

    pdf = fitz.open(p)
    try:
        png = _render_page_png(pdf, 1, dpi=120)
    finally:
        pdf.close()
    assert png[:8] == b"\x89PNG\r\n\x1a\n"      # PNG magic header
    assert len(png) > 100


def test_prompt_includes_vision_note_only_when_attached():
    common = dict(allowed_types="balance_sheet", report_file="r.pdf", report_year=2025,
                  page=4, page_text="Revenue 100")
    _, with_img = prompts.render("extract_tables", vision_note=_VISION_NOTE, **common)
    _, no_img = prompts.render("extract_tables", vision_note="", **common)
    assert "image of this page is attached" in with_img.lower()
    assert "image of this page is attached" not in no_img.lower()
    assert "Revenue 100" in with_img and "Revenue 100" in no_img
