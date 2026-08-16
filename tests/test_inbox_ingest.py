"""Vault Inbox document ingest helpers and API."""

from __future__ import annotations

import base64
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.ingestion.formats import (
    DocumentKind,
    RetrieverKind,
    heuristic_retriever_tags,
    resolve_format,
)
from app.ingestion.inbox import ingest_upload_bytes, prepare_note_filename
from app.obsidian import ObsidianClient
from app.security import Policy, PolicyEngine


@pytest.fixture
def client(
    tmp_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> Iterator[TestClient]:
    """Boot the app against temp settings; stub Obsidian probes."""

    async def available(_self: ObsidianClient) -> bool:
        """Always report plugin offline in tests."""
        return False

    monkeypatch.setattr(ObsidianClient, "available", available)

    from app.main import app

    with TestClient(app) as test_client:
        yield test_client


def _vault_engine(vault: Path) -> PolicyEngine:
    """Build a policy engine sandboxed to a temporary vault."""
    base = PolicyEngine().policy.model_dump()
    base["allow_vault_write"] = True
    base["vault_path"] = str(vault)
    base["allowed_read_paths"] = [str(vault)]
    base["allowed_write_paths"] = [str(vault)]
    base["denied_paths"] = []
    base["require_approval_for"] = []
    tools = set(base.get("allowed_tools") or [])
    tools.update({"vault_write", "vault_read", "note_open"})
    base["allowed_tools"] = sorted(tools)
    return PolicyEngine(Policy.model_validate(base))


def test_resolve_format_routes_by_suffix() -> None:
    """File extensions map to kind + preferred retriever."""
    text = resolve_format("notes.md")
    assert text.kind is DocumentKind.TEXT
    assert text.retriever is RetrieverKind.TEXT_HYBRID

    image = resolve_format("shot.PNG")
    assert image.kind is DocumentKind.IMAGE
    assert image.retriever is RetrieverKind.VISUAL

    pdf = resolve_format("paper.pdf")
    assert pdf.kind is DocumentKind.PDF
    assert pdf.retriever is RetrieverKind.TEXT_HYBRID
    assert pdf.searchable is True

    docx = resolve_format("report.docx")
    assert docx.kind is DocumentKind.DOCX
    assert docx.retriever is RetrieverKind.TEXT_HYBRID
    assert docx.searchable is True

    binary = resolve_format("archive.zip")
    assert binary.kind is DocumentKind.BINARY


def test_extract_docx_text_round_trip() -> None:
    """OOXML paragraph text is extracted from a minimal .docx zip."""
    import io
    import zipfile

    from app.ingestion.docx_extract import extract_docx_text

    xml = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:body>
    <w:p><w:r><w:t>Hello Jarvis DOCX</w:t></w:r></w:p>
  </w:body>
</w:document>"""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as archive:
        archive.writestr("word/document.xml", xml)
    text = extract_docx_text(buf.getvalue())
    assert "Hello Jarvis DOCX" in text


def test_ingest_upload_bytes_docx(tmp_path: Path) -> None:
    """DOCX uploads extract body text into the companion note."""
    import io
    import zipfile

    vault = tmp_path / "vault"
    vault.mkdir()
    engine = _vault_engine(vault)

    xml = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:body>
    <w:p><w:r><w:t>Planet minireport body</w:t></w:r></w:p>
  </w:body>
</w:document>"""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as archive:
        archive.writestr("word/document.xml", xml)
    data = buf.getvalue()

    result = ingest_upload_bytes(engine, filename="report.docx", data=data)
    assert result["kind"] == "docx"
    assert result["retriever"] == "text-hybrid"
    note = (vault / str(result["note_path"])).read_text(encoding="utf-8")
    assert "Planet minireport body" in note
    assert "kind-docx" in note
    assert (vault / str(result["file_path"])).is_file()


def test_extract_pdf_text_round_trip() -> None:
    """pypdf extracts text from a minimal one-page PDF."""
    from app.ingestion.pdf_extract import extract_pdf_text

    minimal = b"""%PDF-1.4
1 0 obj<< /Type /Catalog /Pages 2 0 R >>endobj
2 0 obj<< /Type /Pages /Kids [3 0 R] /Count 1 >>endobj
3 0 obj<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 300] /Contents 4 0 R /Resources<< /Font<< /F1 5 0 R >> >> >>endobj
4 0 obj<< /Length 55 >>stream
BT /F1 12 Tf 50 250 Td (Hello Jarvis PDF) Tj ET
endstream
endobj
5 0 obj<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>endobj
xref
0 6
0000000000 65535 f 
0000000009 00000 n 
0000000058 00000 n 
0000000115 00000 n 
0000000266 00000 n 
0000000371 00000 n 
trailer<< /Size 6 /Root 1 0 R >>
startxref
449
%%EOF
"""
    text = extract_pdf_text(minimal)
    assert "Hello Jarvis PDF" in text


def test_heuristic_retriever_tags_for_images() -> None:
    """Questions about screenshots prefer the visual retriever filter."""
    tags = heuristic_retriever_tags("what does this screenshot say?")
    assert "retriever-visual" in tags
    assert "kind-image" in tags


def test_prepare_note_filename_forces_markdown() -> None:
    """Companion notes are always .md regardless of source type."""
    assert prepare_note_filename("Hello World.txt") == "Hello World.md"
    assert prepare_note_filename("shot.png") == "shot.md"


def test_ingest_upload_bytes_image_and_pdf(tmp_path: Path) -> None:
    """Images and PDFs store originals under Inbox/files/ plus a note."""
    vault = tmp_path / "vault"
    vault.mkdir()
    engine = _vault_engine(vault)

    img = ingest_upload_bytes(
        engine, filename="diagram.png", data=b"\x89PNG\r\n\x1a\nfake"
    )
    assert img["kind"] == "image"
    assert img["retriever"] == "visual"
    assert (vault / str(img["file_path"])).is_file()
    note = (vault / str(img["note_path"])).read_text(encoding="utf-8")
    assert "retriever-visual" in note
    assert "kind-image" in note

    minimal = b"""%PDF-1.4
1 0 obj<< /Type /Catalog /Pages 2 0 R >>endobj
2 0 obj<< /Type /Pages /Kids [3 0 R] /Count 1 >>endobj
3 0 obj<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 300] /Contents 4 0 R /Resources<< /Font<< /F1 5 0 R >> >> >>endobj
4 0 obj<< /Length 55 >>stream
BT /F1 12 Tf 50 250 Td (Steep tea three minutes) Tj ET
endstream
endobj
5 0 obj<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>endobj
xref
0 6
0000000000 65535 f 
0000000009 00000 n 
0000000058 00000 n 
0000000115 00000 n 
0000000266 00000 n 
0000000371 00000 n 
trailer<< /Size 6 /Root 1 0 R >>
startxref
449
%%EOF
"""
    pdf = ingest_upload_bytes(engine, filename="spec.pdf", data=minimal)
    assert pdf["kind"] == "pdf"
    assert pdf["retriever"] == "text-hybrid"
    assert (vault / str(pdf["file_path"])).is_file()
    pdf_note = (vault / str(pdf["note_path"])).read_text(encoding="utf-8")
    assert "Steep tea three minutes" in pdf_note
    assert "kind-pdf" in pdf_note


def test_ingest_paste_api(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """POST /api/notes/ingest writes into the configured vault Inbox."""
    vault = tmp_path / "vault"
    vault.mkdir()
    engine = _vault_engine(vault)
    monkeypatch.setattr("app.main.get_policy_engine", lambda: engine)

    response = client.post(
        "/api/notes/ingest",
        json={"title": "Tea Notes", "content": "Steep for 3 minutes."},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["count"] == 1
    assert body["documents"][0]["retriever"] == "text-hybrid"
    path = body["paths"][0]
    written = (vault / path).read_text(encoding="utf-8")
    assert "Tea Notes" in written


def test_ingest_upload_api_any_type(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Multipart-free base64 upload accepts non-text files."""
    vault = tmp_path / "vault"
    vault.mkdir()
    engine = _vault_engine(vault)
    monkeypatch.setattr("app.main.get_policy_engine", lambda: engine)

    payload = base64.b64encode(b"%PDF-1.4 hello").decode("ascii")
    response = client.post(
        "/api/notes/ingest/upload",
        json={
            "files": [
                {
                    "filename": "brief.pdf",
                    "content_base64": payload,
                    "mime": "application/pdf",
                }
            ]
        },
    )
    assert response.status_code == 200, response.text
    doc = response.json()["documents"][0]
    assert doc["kind"] == "pdf"
    assert doc["retriever"] == "text-hybrid"
    assert doc["file_path"].startswith("Inbox/files/")


def test_ingest_rejects_empty(client: TestClient) -> None:
    """Empty paste content is a 422/400."""
    response = client.post("/api/notes/ingest", json={"content": ""})
    assert response.status_code in {400, 422}
