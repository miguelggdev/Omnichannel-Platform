"""OCR con Tesseract para imagenes y PDFs escaneados.

Contrato: `specs/sprint-05-rag.md` §3. Tesseract y el idioma `spa` ya estan
instalados en la imagen Docker (Sprint 2); ver `Dockerfile`.

La rasterizacion de PDF a imagen usa PyMuPDF (`pymupdf`), no `pdf2image`: este
ultimo depende del binario `poppler-utils`, que no esta en el `Dockerfile`, y
agregarlo ahi es mas invasivo que una dependencia de pip que ya trae su propio
motor de render (MuPDF) sin binarios de sistema adicionales.
"""

import io
from typing import Any

import pymupdf
import pytesseract
from PIL import Image, ImageFilter

# DPI de rasterizado: 300 es el punto de equilibrio estandar para OCR (Tesseract
# recomienda 300-400dpi); un PDF nace a 72dpi.
_OCR_DPI = 300
_PDF_DEFAULT_DPI = 72

# Umbral de binarizacion fijo. Un umbral adaptativo por documento daria mejor
# resultado en fotos con iluminacion despareja, pero para el MVP (escaneos
# planos) un umbral fijo es suficiente y evita una dependencia mas (opencv).
_BINARIZATION_THRESHOLD = 128

_IMAGE_FILE_TYPES = ("png", "jpg", "webp")


class OCRService:
    """Extrae texto de imagenes y PDFs escaneados via Tesseract.

    Attributes:
        language: Idiomas de Tesseract, en su notacion de "+" (e.g. "spa+eng").
    """

    def __init__(self, language: str = "spa+eng") -> None:
        """Configura el idioma de OCR.

        Args:
            language: Codigos de idioma de Tesseract separados por "+".
        """
        self.language = language

    async def extract_text(self, content: bytes, file_type: str) -> list[dict[str, Any]]:
        """Extrae texto via OCR de una imagen o un PDF escaneado.

        Tesseract es sincrono y bloqueante; esto se llama desde el worker de
        Celery (`asyncio.run(...)`), no desde una request HTTP, asi que no hace
        falta despacharlo a un threadpool aqui.

        Args:
            content: Bytes del archivo.
            file_type: Extension normalizada (png, jpg, webp, pdf).

        Returns:
            Una entrada `{"page_number": int, "text": str}` por pagina (una sola
            para imagenes sueltas).

        Raises:
            ValueError: Si `file_type` no es una imagen ni un PDF.
        """
        if file_type in _IMAGE_FILE_TYPES:
            image = Image.open(io.BytesIO(content))
            texto = self._run_tesseract(self._preprocess(image))
            return [{"page_number": 1, "text": texto}]

        if file_type == "pdf":
            paginas = []
            for i, pagina_imagen in enumerate(self._pdf_to_images(content), start=1):
                texto = self._run_tesseract(self._preprocess(pagina_imagen))
                paginas.append({"page_number": i, "text": texto})
            return paginas

        raise ValueError(f"OCRService no soporta el tipo '{file_type}'")

    def _preprocess(self, image: Image.Image) -> Image.Image:
        """Prepara la imagen para mejorar la precision del OCR.

        Escala de grises, binarizacion y un filtro de mediana para limpiar
        ruido de escaneo. Sin correccion de rotacion (deskew): queda para
        cuando haya evidencia real de escaneos torcidos, no antes.

        Args:
            image: Imagen original.

        Returns:
            Imagen en blanco y negro, lista para Tesseract.
        """
        gray = image.convert("L")
        binary = gray.point(lambda x: 255 if x > _BINARIZATION_THRESHOLD else 0)
        return binary.filter(ImageFilter.MedianFilter(size=3))

    def _run_tesseract(self, image: Image.Image) -> str:
        """Ejecuta Tesseract sobre una imagen ya preprocesada.

        Args:
            image: Imagen en blanco y negro.

        Returns:
            Texto reconocido, sin espacios sobrantes al inicio/fin.
        """
        texto: str = pytesseract.image_to_string(
            image,
            lang=self.language,
            config="--oem 3 --psm 6",  # motor LSTM, bloque de texto uniforme
        )
        return texto.strip()

    def _pdf_to_images(self, pdf_content: bytes) -> list[Image.Image]:
        """Rasteriza cada pagina de un PDF a imagen, a 300dpi.

        Args:
            pdf_content: Bytes del PDF.

        Returns:
            Una imagen PIL por pagina, en orden.
        """
        zoom = _OCR_DPI / _PDF_DEFAULT_DPI
        matrix = pymupdf.Matrix(zoom, zoom)

        with pymupdf.open(stream=pdf_content, filetype="pdf") as documento:
            return [
                Image.open(io.BytesIO(pagina.get_pixmap(matrix=matrix).tobytes("png")))
                for pagina in documento
            ]
