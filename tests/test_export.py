"""PDF and Word export.

What matters here is not that a file is produced — it is that the file says the
same things the Markdown does. An export is where a caveat goes quietly missing:
a PDF looks official, a Word document gets edited, and in both cases a reader
who cannot tell which claims are evidenced will assume all of them are. So every
test below is really one question asked four ways: does the export still mark
what it could not verify?

The other half is Vietnamese. A PDF's built-in fonts are Latin-1, so the failure
mode for "Công ty Cổ phần Điện Thủ Đức" is not an error — it is punctuation, in
the one artefact that gets forwarded to other people.
"""

from __future__ import annotations

import io
from datetime import datetime, timezone

import pytest

from companies_research.briefs import to_docx, to_markdown, to_pdf
from companies_research.models import Brief, BriefClaim, MeetingRef


def _brief(**over) -> Brief:
    base = dict(
        lead_id="lead-1",
        company="Công ty Cổ phần Điện Thủ Đức",
        domain="thuduc-electric.vn",
        generated_at=datetime(2026, 8, 24, 9, 30, tzinfo=timezone.utc),
        status="approved",
        approved_by="Christopher Le",
        approved_at=datetime(2026, 8, 24, 9, 45, tzinfo=timezone.utc),
        claims=[
            BriefClaim(field="industry", value="Thiết bị điện công nghiệp",
                       source_url="https://thuduc-electric.vn/about", confidence=0.9),
            BriefClaim(field="size_estimate", value="500–1,000 nhân viên",
                       confidence=0.4),
            BriefClaim(field="news[0]", value="Mở rộng nhà máy tại Bình Dương",
                       source_url="https://vnexpress.net/example", confidence=0.8),
        ],
        talking_points=["Họ đang mở rộng — hỏi về nhu cầu tuyển dụng."],
        unknowns=["Doanh thu 2025 chưa công bố."],
        sources=["https://thuduc-electric.vn"],
    )
    base.update(over)
    return Brief(**base)


def _pdf_text(brief: Brief) -> str:
    pypdf = pytest.importorskip("pypdf")
    reader = pypdf.PdfReader(io.BytesIO(to_pdf(brief)))
    return "\n".join(page.extract_text() for page in reader.pages)


def _docx_text(brief: Brief) -> str:
    docx = pytest.importorskip("docx")
    document = docx.Document(io.BytesIO(to_docx(brief)))
    parts = [p.text for p in document.paragraphs]
    parts += [p.text for s in document.sections for p in s.footer.paragraphs]
    return "\n".join(parts)


# --- the caveat must survive every format -----------------------------------


@pytest.mark.parametrize("extract", [_pdf_text, _docx_text])
def test_an_unsourced_claim_is_still_marked(extract):
    """`size_estimate` has no source_url in the fixture."""
    body = extract(_brief())
    assert "unverified" in body.lower(), "the export dropped the caveat"


@pytest.mark.parametrize("extract", [_pdf_text, _docx_text])
def test_a_sourced_claim_carries_its_url(extract):
    assert "thuduc-electric.vn/about" in extract(_brief())


def test_the_exports_agree_with_the_markdown_on_what_is_verified():
    """Three renderings, one set of facts."""
    brief = _brief()
    markdown = to_markdown(brief)
    for body in (_pdf_text(brief), _docx_text(brief)):
        for claim in brief.claims:
            assert str(claim.value)[:20] in body, f"{claim.field} missing from an export"
        assert ("unverified" in body.lower()) == ("unverified" in markdown.lower()), \
            "an export disagrees with the Markdown about what is evidenced"


# --- Vietnamese --------------------------------------------------------------


@pytest.mark.parametrize("extract", [_pdf_text, _docx_text])
def test_vietnamese_survives_the_round_trip(extract):
    """A Latin-1 font turns this into punctuation, silently."""
    body = extract(_brief())
    for phrase in ("Điện Thủ Đức", "Thiết bị điện công nghiệp", "Bình Dương"):
        assert phrase in body, f"{phrase!r} was mangled"


# --- structure ---------------------------------------------------------------


def test_the_pdf_has_a_running_header_and_numbered_footer():
    """A multi-page brief must say which company page 3 belongs to."""
    pypdf = pytest.importorskip("pypdf")
    brief = _brief(claims=[
        BriefClaim(field=f"meeting_prep[{i}]", value=f"Điểm thảo luận số {i} " + "x" * 400,
                   confidence=0.6)
        for i in range(12)
    ])
    reader = pypdf.PdfReader(io.BytesIO(to_pdf(brief)))
    assert len(reader.pages) > 1, "the fixture did not span pages"
    later = reader.pages[1].extract_text()
    assert "COMPANY BRIEF" in later, "no running header on page 2"
    assert "2 /" in later, "no page number on page 2"


def test_the_approval_is_stamped_on_the_document():
    """Who approved it travels with it, not just in the database."""
    for body in (_pdf_text(_brief()), _docx_text(_brief())):
        assert "Christopher Le" in body


def test_a_draft_says_so():
    body = _pdf_text(_brief(status="draft", approved_by="", approved_at=None))
    assert "DRAFT" in body


def test_the_word_file_uses_real_styles_so_it_can_be_edited():
    """Direct formatting does not survive someone restyling the document."""
    docx = pytest.importorskip("docx")
    document = docx.Document(io.BytesIO(to_docx(_brief())))
    styles = {p.style.name for p in document.paragraphs}
    assert "Title" in styles
    assert any(s.startswith("Heading") for s in styles)
    assert "List Bullet" in styles


def test_both_exports_are_plausible_files():
    assert to_pdf(_brief())[:5] == b"%PDF-"
    assert to_docx(_brief())[:2] == b"PK", "a .docx is a zip"


def test_an_empty_brief_does_not_crash_either_exporter():
    bare = Brief(lead_id="x", company="", domain="",
                 generated_at=datetime(2026, 1, 1, tzinfo=timezone.utc))
    assert to_pdf(bare)[:5] == b"%PDF-"
    assert to_docx(bare)[:2] == b"PK"


def test_a_meeting_appears_in_both_exports():
    brief = _brief(upcoming_meeting=MeetingRef(
        event_id="evt-1",
        title="Họp giới thiệu giải pháp",
        starts_at=datetime(2026, 9, 1, 9, 0, tzinfo=timezone.utc),
        matched_on="attendee_domain",
        confidence=0.9,
    ))
    for body in (_pdf_text(brief), _docx_text(brief)):
        assert "Họp giới thiệu giải pháp" in body


def test_the_email_sentinel_is_never_shown_as_a_clickable_source():
    """`email:<id>` is provenance, not a URL anyone can open."""
    brief = _brief(claims=[
        BriefClaim(field="contact_name", value="Đặng Quỳnh Anh",
                   source_url="email:19fd00cca3c811be", confidence=0.9),
    ])
    for body in (_pdf_text(brief), _docx_text(brief)):
        assert "email:19fd" not in body, "a raw message id leaked into the export"
        assert "from the email" in body


def test_all_three_renderings_use_the_same_words_for_provenance():
    from companies_research.briefs.render import _source_note

    assert _source_note(BriefClaim(field="x", value="v", confidence=0.5)) == ("unverified", True)
    assert _source_note(BriefClaim(field="x", value="v", source_url="email:abc",
                                   confidence=0.5)) == ("from the email", False)
    assert _source_note(BriefClaim(field="x", value="v", source_url="https://a.example",
                                   confidence=0.5)) == ("https://a.example", False)
