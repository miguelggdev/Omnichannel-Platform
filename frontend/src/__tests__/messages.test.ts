import { describe, expect, it } from "vitest";
import de from "../../messages/de.json";
import en from "../../messages/en.json";
import es from "../../messages/es.json";
import fr from "../../messages/fr.json";
import it_ from "../../messages/it.json";
import pt from "../../messages/pt.json";
import { LOCALES } from "@/lib/constants";

const ALL: Record<string, unknown> = { es, en, pt, it: it_, de, fr };

function flatten(node: unknown, prefix = ""): Record<string, string> {
  const out: Record<string, string> = {};
  for (const [k, v] of Object.entries(node as Record<string, unknown>)) {
    const key = prefix ? `${prefix}.${k}` : k;
    if (typeof v === "string") out[key] = v;
    else Object.assign(out, flatten(v, key));
  }
  return out;
}

/** Nombres de los parametros ICU de un texto: "{page} de {pages}" -> ["page", "pages"]. */
function params(texto: string): string[] {
  // Se quitan los tramos entre apostrofes ('{{x}}' es texto literal, no un parametro).
  const sinLiterales = texto.replace(/'[^']*'/g, "");
  return [...sinLiterales.matchAll(/\{(\w+)\}/g)].map((m) => m[1] as string).sort();
}

const base = flatten(es);

describe("traducciones", () => {
  it("hay un fichero por cada idioma que la app declara", () => {
    expect(Object.keys(ALL).sort()).toEqual([...LOCALES].sort());
  });

  it.each(LOCALES)("%s tiene exactamente las mismas claves que es", (locale) => {
    const claves = Object.keys(flatten(ALL[locale]));
    expect(claves.sort()).toEqual(Object.keys(base).sort());
  });

  it.each(LOCALES)("%s no tiene textos vacios", (locale) => {
    const vacias = Object.entries(flatten(ALL[locale])).filter(([, v]) => v.trim() === "");
    expect(vacias).toEqual([]);
  });

  it.each(LOCALES)("%s usa los mismos parametros que es en cada texto", (locale) => {
    const t = flatten(ALL[locale]);
    for (const [clave, texto] of Object.entries(base)) {
      expect(params(t[clave] ?? ""), `${locale}:${clave}`).toEqual(params(texto));
    }
  });

  it.each(LOCALES.filter((l) => l !== "es"))("%s esta realmente traducido, no copiado de es", (locale) => {
    const t = flatten(ALL[locale]);
    const iguales = Object.keys(base).filter((k) => t[k] === base[k] && (base[k] ?? "").length > 25);
    // Los textos largos no pueden coincidir con el espanol por casualidad.
    expect(iguales).toEqual([]);
  });
});
