"""Topes de la ingesta frente a archivos hostiles (ADR-088)."""

import io
import zipfile

import pymupdf
import pytest
from docx import Document as DocxFile

from app.services import document_pipeline as dp
from app.services import ocr


def _docx(texto: str = "Hola") -> bytes:
    salida = io.BytesIO()
    documento = DocxFile()
    documento.add_paragraph(texto)
    documento.save(salida)
    return salida.getvalue()


def _bomba(tamano: int) -> bytes:
    """Zip con una entrada de `tamano` ceros: comprime miles de veces."""
    salida = io.BytesIO()
    with zipfile.ZipFile(salida, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("word/document.xml", b"\0" * tamano)
    return salida.getvalue()


def _pdf(paginas: int) -> bytes:
    documento = pymupdf.open()
    for _ in range(paginas):
        documento.new_page()
    return bytes(documento.tobytes())


class TestZip:
    def test_un_docx_normal_pasa(self) -> None:
        assert "Hola" in dp.extract_docx_text(_docx())[0]["text"]

    def test_una_bomba_de_descompresion_se_rechaza(self) -> None:
        with pytest.raises(dp.DocumentRejectedError, match="descomprime"):
            dp.extract_docx_text(_bomba(5 * 1024 * 1024))

    def test_tambien_en_xlsx(self) -> None:
        with pytest.raises(dp.DocumentRejectedError):
            dp.extract_xlsx_text(_bomba(5 * 1024 * 1024))

    def test_un_archivo_que_no_es_zip_se_rechaza(self) -> None:
        with pytest.raises(dp.DocumentRejectedError, match="Office"):
            dp.extract_docx_text(b"no soy un docx")

    def test_demasiadas_entradas(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(dp, "MAX_ZIP_ENTRIES", 3)
        with pytest.raises(dp.DocumentRejectedError, match="partes"):
            dp.extract_docx_text(_docx())


class TestPdf:
    def test_un_pdf_con_demasiadas_paginas_se_rechaza(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(dp, "MAX_PDF_PAGES", 3)
        assert len(dp.extract_pdf_pages(_pdf(3))) == 3
        with pytest.raises(dp.DocumentRejectedError, match="paginas"):
            dp.extract_pdf_pages(_pdf(4))

    def test_el_ocr_no_rasteriza_un_pdf_enorme(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(ocr, "MAX_OCR_PAGES", 2)
        with pytest.raises(dp.DocumentRejectedError, match="escaneado"):
            ocr.OCRService()._pdf_to_images(_pdf(3))
