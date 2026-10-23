import { describe, expect, it } from "vitest";
import { urlSegura } from "@/lib/utils";

describe("urlSegura", () => {
  it("acepta http y https", () => {
    expect(urlSegura("https://cdn.example.com/a.png")).toBe("https://cdn.example.com/a.png");
    expect(urlSegura("http://example.com/x")).toBe("http://example.com/x");
  });

  it.each(["javascript:alert(1)", "JaVaScRiPt:alert(1)", "data:text/html,<script>1</script>", "tg-file:123", "no es url", "", null, undefined])(
    "rechaza %s",
    (valor) => {
      expect(urlSegura(valor as string | null | undefined)).toBeNull();
    },
  );
});
