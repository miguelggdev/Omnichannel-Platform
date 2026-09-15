"""Chunking inteligente de texto extraido para RAG.

Contrato: `specs/sprint-05-rag.md` §4. Verificado por
`tests/unit/test_rag.py::TestChunking`.

El tamano de chunk (`chunk_size`/`chunk_overlap`) se mide en **caracteres**, no en
tokens: es lo que espera `RecursiveCharacterTextSplitter` por defecto y lo que
verifica el test de contrato (un chunk de `chunk_size=500` no debe superar ~600
caracteres). `_count_tokens()` se usa aparte, solo para poblar
`ChunkResult.token_count` — que consume el pipeline para guardarlo en
`document_chunks.token_count`, no para decidir donde cortar.
"""

import re
from dataclasses import dataclass
from typing import Any

import tiktoken
from langchain_text_splitters import RecursiveCharacterTextSplitter

# Umbral de coincidencias para considerar un texto como FAQ: necesita al menos
# una marca de pregunta y una de respuesta (o la palabra "FAQ"/"preguntas
# frecuentes"), no basta con una sola.
_FAQ_PATTERNS = (
    r"(?:P(?:regunta)?|Q)\s*[:.]",
    r"(?:R(?:espuesta)?|A)\s*[:.]",
    r"FAQ",
    r"[Pp]reguntas [Ff]recuentes",
)
_FAQ_MIN_MATCHES = 2

# Patron de pares pregunta/respuesta: "P: ... R: ...", "Pregunta: ... Respuesta: ...",
# "Q: ... A: ...". El lookahead corta antes del siguiente marcador o al final.
_FAQ_PAIR_PATTERN = re.compile(
    r"(?:P(?:regunta)?|Q)\s*[:.]\s*(.*?)\s*(?:R(?:espuesta)?|A)\s*[:.]\s*(.*?)"
    r"(?=(?:P(?:regunta)?|Q)\s*[:.]|$)",
    re.DOTALL | re.IGNORECASE,
)

# Minimo de lineas con "|" o tabulador para considerar que hay una tabla.
_TABLE_MIN_LINES = 3


@dataclass
class ChunkResult:
    """Un fragmento de texto listo para embeber.

    Attributes:
        content: Texto del chunk.
        page_number: Pagina de origen, si el formato la tiene.
        section: Etiqueta de seccion ("FAQ", "Tabla", o None para texto normal).
        token_count: Tokens del chunk segun el encoding de OpenAI.
    """

    content: str
    page_number: int | None
    section: str | None
    token_count: int


class DocumentChunker:
    """Divide texto extraido en chunks, con estrategias por tipo de contenido.

    Estrategias:
        - FAQ: un chunk por par pregunta-respuesta.
        - Tablas: la tabla completa como un solo chunk, aparte del texto.
        - Texto general: `RecursiveCharacterTextSplitter` (chunk_size/chunk_overlap
          en caracteres).
    """

    def __init__(
        self,
        chunk_size: int = 1000,
        chunk_overlap: int = 200,
        encoding_name: str = "cl100k_base",
    ) -> None:
        """Configura el chunker.

        Args:
            chunk_size: Tamano maximo de chunk, en caracteres.
            chunk_overlap: Solapamiento entre chunks consecutivos, en caracteres.
            encoding_name: Encoding de tiktoken para contar tokens (metadata, no
                afecta donde se corta).
        """
        self.chunk_size = chunk_size
        self.chunk_overlap = chunk_overlap
        self.encoding = tiktoken.get_encoding(encoding_name)
        self.splitter = RecursiveCharacterTextSplitter(
            chunk_size=chunk_size,
            chunk_overlap=chunk_overlap,
            separators=["\n\n", "\n", ". ", " ", ""],
        )

    def chunk(
        self,
        text_pages: list[dict[str, Any]],
        document_title: str,
        file_type: str,
    ) -> list[ChunkResult]:
        """Divide el texto de todas las paginas en chunks.

        Args:
            text_pages: Paginas extraidas, cada una `{"page_number": int | None,
                "text": str}`.
            document_title: Titulo del documento (para metadata futura).
            file_type: Extension del archivo (no cambia la estrategia hoy, pero
                queda disponible para reglas por tipo mas adelante).

        Returns:
            Chunks de todas las paginas con contenido, en orden.
        """
        all_chunks: list[ChunkResult] = []

        for page in text_pages:
            page_num = page.get("page_number")
            text = page.get("text", "") or ""

            if not text.strip():
                continue

            if self._is_faq(text):
                chunks = self._chunk_faq(text, page_num)
            elif self._contains_table(text):
                chunks = self._chunk_with_tables(text, page_num)
            else:
                chunks = self._chunk_text(text, page_num)

            all_chunks.extend(chunks)

        return all_chunks

    def _chunk_text(self, text: str, page_number: int | None) -> list[ChunkResult]:
        """Chunking estandar con `RecursiveCharacterTextSplitter`.

        Args:
            text: Texto a dividir.
            page_number: Pagina de origen.

        Returns:
            Un `ChunkResult` por fragmento; lista vacia si `text` esta vacio.
        """
        if not text.strip():
            return []

        return [
            ChunkResult(
                content=split,
                page_number=page_number,
                section=None,
                token_count=self._count_tokens(split),
            )
            for split in self.splitter.split_text(text)
        ]

    def _chunk_faq(self, text: str, page_number: int | None) -> list[ChunkResult]:
        """Un chunk por par pregunta-respuesta.

        Args:
            text: Texto con estructura de FAQ.
            page_number: Pagina de origen.

        Returns:
            Un chunk por par detectado; si no se detecta ninguno, cae al
            chunking estandar en vez de perder el contenido.
        """
        matches = _FAQ_PAIR_PATTERN.findall(text)
        if not matches:
            return self._chunk_text(text, page_number)

        chunks = []
        for question, answer in matches:
            content = f"Pregunta: {question.strip()}\nRespuesta: {answer.strip()}"
            chunks.append(
                ChunkResult(
                    content=content,
                    page_number=page_number,
                    section="FAQ",
                    token_count=self._count_tokens(content),
                )
            )
        return chunks

    def _chunk_with_tables(self, text: str, page_number: int | None) -> list[ChunkResult]:
        """Preserva las tablas como chunks completos, separadas del texto normal.

        Deteccion basica: lineas con "|" o tabulador se agrupan como tabla; el
        resto pasa por el chunking estandar.

        Args:
            text: Texto que contiene al menos una tabla.
            page_number: Pagina de origen.

        Returns:
            Chunks de tabla (`section="Tabla"`) intercalados con chunks de texto,
            en el orden en que aparecen.
        """
        chunks: list[ChunkResult] = []
        current_table: list[str] = []
        current_text: list[str] = []

        def _flush_table() -> None:
            if not current_table:
                return
            content = "\n".join(current_table)
            chunks.append(
                ChunkResult(
                    content=content,
                    page_number=page_number,
                    section="Tabla",
                    token_count=self._count_tokens(content),
                )
            )
            current_table.clear()

        def _flush_text() -> None:
            if not current_text:
                return
            chunks.extend(self._chunk_text("\n".join(current_text), page_number))
            current_text.clear()

        for line in text.split("\n"):
            if "|" in line or "\t" in line:
                _flush_text()
                current_table.append(line)
            else:
                _flush_table()
                current_text.append(line)

        _flush_table()
        _flush_text()

        return chunks

    def _is_faq(self, text: str) -> bool:
        """Detecta estructura de FAQ por la cantidad de marcadores presentes.

        Args:
            text: Texto a inspeccionar.

        Returns:
            True si aparecen al menos `_FAQ_MIN_MATCHES` patrones distintos.
        """
        matches = sum(1 for pattern in _FAQ_PATTERNS if re.search(pattern, text))
        return matches >= _FAQ_MIN_MATCHES

    def _contains_table(self, text: str) -> bool:
        """Detecta si el texto tiene una tabla en formato de pipes.

        Args:
            text: Texto a inspeccionar.

        Returns:
            True si hay al menos `_TABLE_MIN_LINES` lineas con 2+ "|".
        """
        pipe_lines = sum(1 for line in text.split("\n") if line.count("|") >= 2)
        return pipe_lines >= _TABLE_MIN_LINES

    def _count_tokens(self, text: str) -> int:
        """Cuenta tokens con el encoding de OpenAI configurado.

        Args:
            text: Texto a medir.

        Returns:
            Numero de tokens.
        """
        return len(self.encoding.encode(text))
