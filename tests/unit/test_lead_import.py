"""Parseo del CSV de importacion de leads: codificaciones, cabeceras, limites y errores por fila."""

import pytest

from app.services.lead_import import (
    MAX_FILAS,
    CsvError,
    normalizar_cabecera,
    normalizar_linkedin,
    parsear_csv,
)


def _csv(texto: str, codificacion: str = "utf-8") -> bytes:
    return texto.encode(codificacion)


class TestCabeceras:
    @pytest.mark.parametrize(
        ("original", "esperado"),
        [
            ("Teléfono (móvil)", "telefonomovil"),
            ("E-mail", "email"),
            (" Job Title ", "jobtitle"),
            ("ÑOÑO", "nono"),
        ],
    )
    def test_normalizar(self, original: str, esperado: str) -> None:
        assert normalizar_cabecera(original) == esperado

    def test_alias_en_espanol_e_ingles_y_de_linkedin(self) -> None:
        r = parsear_csv(
            _csv(
                "Nombre,Apellido,Correo,Celular,Empresa,Cargo,Sector,profileUrl\n"
                "Ana,Gomez,ana@example.com,+57 300 111-2233,ACME,CTO,Software,https://linkedin.com/in/ana\n"
            )
        )
        (fila,) = r.filas
        assert fila.error is None
        assert fila.datos == {
            "first_name": "Ana",
            "last_name": "Gomez",
            "email": "ana@example.com",
            "phone": "+57 300 111-2233",
            "company_name": "ACME",
            "job_title": "CTO",
            "industry": "Software",
            "linkedin_url": "https://linkedin.com/in/ana",
        }
        assert r.columnas_ignoradas == []

    def test_nombre_completo_se_divide_y_no_pisa_un_nombre_explicito(self) -> None:
        r = parsear_csv(_csv("fullName,email\nAna María Gómez Ruiz,a@example.com\n"))
        assert (r.filas[0].datos["first_name"], r.filas[0].datos["last_name"]) == (
            "Ana",
            "María Gómez Ruiz",
        )
        r = parsear_csv(
            _csv("Name,Nombre,Apellido,email\nIgnorado Del Todo,Ana,Perez,a@example.com\n")
        )
        assert (r.filas[0].datos["first_name"], r.filas[0].datos["last_name"]) == ("Ana", "Perez")

    def test_las_columnas_desconocidas_se_ignoran_y_se_informan(self) -> None:
        r = parsear_csv(_csv("email,color favorito,notas\na@example.com,azul,hola\n"))
        assert r.columnas_ignoradas == ["color favorito", "notas"]
        assert r.filas[0].datos == {"email": "a@example.com"}

    def test_una_cabecera_repetida_cuenta_una_vez(self) -> None:
        r = parsear_csv(_csv("email,mail,nombre\nuno@example.com,dos@example.com,Ana\n"))
        assert r.filas[0].datos["email"] == "uno@example.com"
        assert r.columnas_ignoradas == ["mail"]


class TestFormato:
    @pytest.mark.parametrize("delimitador", [",", ";", "\t"])
    def test_delimitadores(self, delimitador: str) -> None:
        texto = (
            delimitador.join(["email", "nombre"])
            + "\n"
            + delimitador.join(["a@example.com", "Ana"])
            + "\n"
        )
        assert parsear_csv(_csv(texto)).filas[0].datos == {
            "email": "a@example.com",
            "first_name": "Ana",
        }

    def test_utf8_con_bom_y_latin1_de_excel(self) -> None:
        con_bom = parsear_csv(b"\xef\xbb\xbf" + _csv("email,nombre\na@example.com,José\n"))
        latin = parsear_csv(_csv("email;nombre\na@example.com;José\n", "latin-1"))
        assert con_bom.filas[0].datos["first_name"] == "José"
        assert latin.filas[0].datos["first_name"] == "José"

    def test_campos_entre_comillas_con_el_delimitador_dentro(self) -> None:
        r = parsear_csv(_csv('email,empresa\na@example.com,"Gomez, Perez y Asociados"\n'))
        assert r.filas[0].datos["company_name"] == "Gomez, Perez y Asociados"

    def test_filas_en_blanco_se_saltan_y_los_numeros_cuentan_la_cabecera(self) -> None:
        r = parsear_csv(_csv("email\na@example.com\n\n,\nb@example.com\n"))
        assert [f.numero for f in r.filas] == [2, 5]

    def test_filas_cortas_no_rompen(self) -> None:
        r = parsear_csv(_csv("nombre,email,empresa\nAna,a@example.com\n"))
        assert r.filas[0].datos == {"first_name": "Ana", "email": "a@example.com"}


class TestValidacionPorFila:
    def test_una_fila_mala_no_tumba_a_las_demas_y_el_error_no_repite_el_dato(self) -> None:
        r = parsear_csv(
            _csv(
                "nombre,email,telefono\n"
                "Ana,correo-con-datos-privados,\n"
                "Beto,b@example.com,\n"
                "Cata,,TELEFONO-INVALIDO-SECRETO\n"
                "Dani,,\n"
            )
        )
        errores = {f.numero: f.error for f in r.filas if f.error}
        assert set(errores) == {2, 4, 5}
        assert errores[2] == "email no es valido"
        assert errores[4] == "phone no es valido"
        assert errores[5] == "falta email, telefono o LinkedIn"
        for mensaje in errores.values():
            assert "privados" not in mensaje
            assert "SECRETO" not in mensaje
        assert [f.numero for f in r.filas if not f.error] == [3]

    @pytest.mark.parametrize(
        ("original", "esperado"),
        [
            ("http://linkedin.com/in/ana", "https://linkedin.com/in/ana"),
            ("www.linkedin.com/in/ana", "https://www.linkedin.com/in/ana"),
            ("https://linkedin.com/in/ana", "https://linkedin.com/in/ana"),
            ("", ""),
        ],
    )
    def test_linkedin_se_lleva_a_https(self, original: str, esperado: str) -> None:
        assert normalizar_linkedin(original) == esperado

    def test_un_esquema_peligroso_no_se_convierte_en_valido(self) -> None:
        r = parsear_csv(_csv("linkedin\njavascript:alert(1)\n"))
        # Se antepone https://, asi que nunca queda un `javascript:` en la URL guardada.
        assert r.filas[0].datos["linkedin_url"].startswith("https://")


class TestLimites:
    def test_vacio_o_solo_cabecera(self) -> None:
        for contenido in (b"", b"   \n  ", _csv("email\n")):
            with pytest.raises(CsvError):
                parsear_csv(contenido)

    def test_sin_cabeceras_reconocibles_o_sin_columna_de_contacto(self) -> None:
        with pytest.raises(CsvError, match="No se reconoce"):
            parsear_csv(_csv("foo,bar\n1,2\n"))
        with pytest.raises(CsvError, match="Falta una columna de contacto"):
            parsear_csv(_csv("nombre,empresa\nAna,ACME\n"))

    def test_demasiadas_filas(self) -> None:
        cuerpo = "email\n" + "".join(f"u{i}@example.com\n" for i in range(MAX_FILAS + 1))
        with pytest.raises(CsvError, match="filas"):
            parsear_csv(_csv(cuerpo))
        ok = "email\n" + "".join(f"u{i}@example.com\n" for i in range(MAX_FILAS))
        assert len(parsear_csv(_csv(ok)).filas) == MAX_FILAS

    def test_demasiado_grande(self) -> None:
        with pytest.raises(CsvError, match="MB"):
            parsear_csv(b"email\n" + b"a" * (2 * 1024 * 1024 + 1))
