import { describe, expect, it } from "vitest";
import { buildUpdate, profileSchema, toFormValues } from "@/lib/business-profile";
import { DAYS, type BusinessProfile } from "@/types";

const horario = Object.fromEntries(
  DAYS.map((d) => [d, { is_open: d !== "saturday" && d !== "sunday", open_time: "08:00", close_time: "18:00" }]),
) as BusinessProfile["operating_hours"];

const perfil = (over: Partial<BusinessProfile> = {}): BusinessProfile => ({
  business_name: "Clinica Sol",
  business_type: null,
  description: null,
  phone: null,
  email: null,
  website: null,
  address: null,
  city: "Cali",
  country: "CO",
  timezone: "America/Bogota",
  social_media: { instagram: "clinicasol" },
  operating_hours: horario,
  primary_color: "#1d4ed8",
  secondary_color: null,
  logo_url: null,
  welcome_message: "Hola",
  handoff_message: null,
  has_agent: true,
  ...over,
});

describe("buildUpdate: solo viaja lo que cambia", () => {
  it("sin cambios, el cuerpo esta vacio", () => {
    const v = toFormValues(perfil());

    expect(buildUpdate(v, structuredClone(v))).toEqual({});
  });

  it("un campo de texto cambiado viaja recortado; los demas no", () => {
    const inicial = toFormValues(perfil());
    const actual = { ...structuredClone(inicial), city: "  Bogota  " };

    expect(buildUpdate(inicial, actual)).toEqual({ city: "Bogota" });
  });

  it("vaciar un campo lo manda como null (borrar)", () => {
    const inicial = toFormValues(perfil());
    const actual = { ...structuredClone(inicial), city: "", welcome_message: "   " };

    expect(buildUpdate(inicial, actual)).toEqual({ city: null, welcome_message: null });
  });

  it("un campo que sigue vacio no viaja", () => {
    const inicial = toFormValues(perfil());
    const actual = { ...structuredClone(inicial), description: "  " };

    expect(buildUpdate(inicial, actual)).toEqual({});
  });

  it("las redes se comparan una a una y vaciar una la borra", () => {
    const inicial = toFormValues(perfil());
    const actual = structuredClone(inicial);
    actual.social.twitter = "nuevo";
    actual.social.instagram = "";

    expect(buildUpdate(inicial, actual)).toEqual({ social_media: { twitter: "nuevo", instagram: null } });
  });

  it("solo viajan los dias que cambiaron", () => {
    const inicial = toFormValues(perfil());
    const actual = structuredClone(inicial);
    actual.hours.saturday = { is_open: true, open_time: "09:00", close_time: "13:00" };

    expect(buildUpdate(inicial, actual)).toEqual({
      operating_hours: { saturday: { is_open: true, open_time: "09:00", close_time: "13:00" } },
    });
  });
});

describe("profileSchema: espejo de las validaciones del backend", () => {
  const base = () => toFormValues(perfil());
  const mensajes = (v: unknown) => {
    const r = profileSchema.safeParse(v);
    return r.success ? [] : r.error.issues.map((i) => `${i.path.join(".")}:${i.message}`);
  };

  it("un perfil correcto valida", () => {
    expect(mensajes(base())).toEqual([]);
  });

  it.each([
    [{ primary_color: "azul" }, "primary_color:color"],
    [{ primary_color: "#12345" }, "primary_color:color"],
    [{ phone: "abc" }, "phone:phone"],
    [{ email: "no-es-correo" }, "email:email"],
    [{ website: "javascript:alert(1)" }, "website:url"],
    [{ logo_url: "ftp://x.example.com" }, "logo_url:url"],
    [{ country: "COL" }, "country:country"],
    [{ timezone: "Mars/Olympus" }, "timezone:timezone"],
    [{ business_name: "   " }, "business_name:required"],
  ])("rechaza %j", (cambio, esperado) => {
    expect(mensajes({ ...base(), ...cambio })).toContain(esperado);
  });

  it("acepta los campos opcionales vacios", () => {
    const v = { ...base(), phone: "", email: "", website: "", primary_color: "", country: "", timezone: "" };

    expect(mensajes(v)).toEqual([]);
  });

  it("un dia abierto necesita cierre posterior a la apertura", () => {
    const v = base();
    v.hours.monday = { is_open: true, open_time: "18:00", close_time: "08:00" };

    expect(mensajes(v)).toContain("hours.monday.close_time:closeAfterOpen");
  });

  it("un dia cerrado no se valida", () => {
    const v = base();
    v.hours.monday = { is_open: false, open_time: "", close_time: "" };

    expect(mensajes(v)).toEqual([]);
  });

  it("un handle de instagram admite @ y rechaza espacios", () => {
    const ok = base();
    ok.social.instagram = "@clinica.sol";
    const mal = base();
    mal.social.instagram = "con espacios";

    expect(mensajes(ok)).toEqual([]);
    expect(mensajes(mal)).toContain("social.instagram:handle");
  });
});
