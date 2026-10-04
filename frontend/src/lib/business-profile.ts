import { z } from "zod";
import {
  DAYS,
  SOCIAL_NETWORKS,
  type BusinessProfile,
  type BusinessProfileUpdate,
  type Day,
  type SocialNetwork,
} from "@/types";

/** Valores del formulario: todo texto, `""` = vacio (el backend usa `null`). */
export interface ProfileFormValues {
  business_name: string;
  business_type: string;
  description: string;
  phone: string;
  email: string;
  website: string;
  address: string;
  city: string;
  country: string;
  timezone: string;
  primary_color: string;
  secondary_color: string;
  logo_url: string;
  welcome_message: string;
  handoff_message: string;
  social: Record<SocialNetwork, string>;
  hours: Record<Day, { is_open: boolean; open_time: string; close_time: string }>;
}

const CAMPOS_DE_TEXTO = [
  "business_type",
  "description",
  "phone",
  "email",
  "website",
  "address",
  "city",
  "country",
  "timezone",
  "primary_color",
  "secondary_color",
  "logo_url",
  "welcome_message",
  "handoff_message",
] as const;

export function toFormValues(p: BusinessProfile): ProfileFormValues {
  const texto = (v: string | null) => v ?? "";
  return {
    business_name: p.business_name,
    business_type: texto(p.business_type),
    description: texto(p.description),
    phone: texto(p.phone),
    email: texto(p.email),
    website: texto(p.website),
    address: texto(p.address),
    city: texto(p.city),
    country: texto(p.country),
    timezone: texto(p.timezone),
    primary_color: texto(p.primary_color),
    secondary_color: texto(p.secondary_color),
    logo_url: texto(p.logo_url),
    welcome_message: texto(p.welcome_message),
    handoff_message: texto(p.handoff_message),
    social: Object.fromEntries(SOCIAL_NETWORKS.map((r) => [r, p.social_media[r] ?? ""])) as Record<SocialNetwork, string>,
    hours: Object.fromEntries(
      DAYS.map((d) => [
        d,
        {
          is_open: p.operating_hours[d].is_open,
          open_time: p.operating_hours[d].open_time ?? "08:00",
          close_time: p.operating_hours[d].close_time ?? "18:00",
        },
      ]),
    ) as ProfileFormValues["hours"],
  };
}

/**
 * Cuerpo del `PUT`: solo lo que cambio respecto a `initial`. Un texto que se vacia viaja como
 * `null` (borrar); lo que no se toca no viaja, asi que no pisa lo que otra persona haya guardado
 * entre tanto en otros campos. Los dias y las redes se comparan uno a uno.
 */
export function buildUpdate(initial: ProfileFormValues, current: ProfileFormValues): BusinessProfileUpdate {
  const out: BusinessProfileUpdate = {};

  if (current.business_name.trim() !== initial.business_name) out.business_name = current.business_name.trim();
  for (const campo of CAMPOS_DE_TEXTO) {
    if (current[campo].trim() !== initial[campo].trim()) {
      out[campo] = current[campo].trim() === "" ? null : current[campo].trim();
    }
  }

  const redes: NonNullable<BusinessProfileUpdate["social_media"]> = {};
  for (const red of SOCIAL_NETWORKS) {
    if (current.social[red].trim() !== initial.social[red].trim()) {
      redes[red] = current.social[red].trim() === "" ? null : current.social[red].trim();
    }
  }
  if (Object.keys(redes).length > 0) out.social_media = redes;

  const dias: NonNullable<BusinessProfileUpdate["operating_hours"]> = {};
  for (const dia of DAYS) {
    const a = initial.hours[dia];
    const b = current.hours[dia];
    if (a.is_open !== b.is_open || a.open_time !== b.open_time || a.close_time !== b.close_time) {
      dias[dia] = { is_open: b.is_open, open_time: b.open_time, close_time: b.close_time };
    }
  }
  if (Object.keys(dias).length > 0) out.operating_hours = dias;

  return out;
}

const HORA = /^([01]\d|2[0-3]):[0-5]\d$/;
const COLOR = /^#[0-9a-fA-F]{6}$/;
const TELEFONO = /^\+?[0-9 ()-]{6,25}$/;
const HANDLE = /^@?[A-Za-z0-9._]{1,50}$/;

function zonaValida(z: string): boolean {
  try {
    new Intl.DateTimeFormat("en", { timeZone: z });
    return true;
  } catch {
    return false;
  }
}

const url = z.string().refine((v) => v === "" || /^https?:\/\/\S+$/i.test(v), "url");
const vacioOk = (re: RegExp, msg: string) => z.string().refine((v) => v === "" || re.test(v), msg);

const dia = z
  .object({ is_open: z.boolean(), open_time: z.string(), close_time: z.string() })
  .superRefine((d, ctx) => {
    if (!d.is_open) return;
    if (!HORA.test(d.open_time)) ctx.addIssue({ code: "custom", path: ["open_time"], message: "time" });
    if (!HORA.test(d.close_time)) ctx.addIssue({ code: "custom", path: ["close_time"], message: "time" });
    else if (HORA.test(d.open_time) && d.close_time <= d.open_time) {
      ctx.addIssue({ code: "custom", path: ["close_time"], message: "closeAfterOpen" });
    }
  });

/** Espejo de `BusinessProfileUpdate` del backend; los mensajes son claves de `businessProfile.errors`. */
export const profileSchema = z.object({
  business_name: z.string().trim().min(1, "required").max(255, "tooLong"),
  business_type: z.string().max(100, "tooLong"),
  description: z.string().max(1000, "tooLong"),
  phone: vacioOk(TELEFONO, "phone"),
  email: z.string().refine((v) => v === "" || z.string().email().safeParse(v).success, "email"),
  website: url,
  address: z.string().max(300, "tooLong"),
  city: z.string().max(100, "tooLong"),
  country: vacioOk(/^[A-Za-z]{2}$/, "country"),
  timezone: z.string().refine((v) => v === "" || zonaValida(v), "timezone"),
  primary_color: vacioOk(COLOR, "color"),
  secondary_color: vacioOk(COLOR, "color"),
  logo_url: url,
  welcome_message: z.string().max(1000, "tooLong"),
  handoff_message: z.string().max(1000, "tooLong"),
  social: z.object({
    instagram: vacioOk(HANDLE, "handle"),
    twitter: vacioOk(HANDLE, "handle"),
    facebook: url,
    whatsapp: vacioOk(TELEFONO, "phone"),
  }),
  hours: z.object(Object.fromEntries(DAYS.map((d) => [d, dia])) as Record<Day, typeof dia>),
});
