import { z } from "zod";
import { BUSINESS_TYPES } from "@/types";

/** Paises que se ofrecen al registrarse (codigo ISO 3166-1 alfa-2). El nombre sale de `Intl`. */
export const COUNTRIES = [
  "CO", "MX", "AR", "CL", "PE", "EC", "UY", "PY", "BO", "VE", "CR", "PA", "DO", "GT", "HN", "SV",
  "NI", "ES", "PT", "BR", "US", "GB", "FR", "DE", "IT",
] as const;

/** Espejo de las reglas del backend: 8-72 caracteres con mayuscula, minuscula y numero. */
export const passwordSchema = z
  .string()
  .min(8, "passwordMin")
  .max(72, "passwordMax")
  .refine((v) => /[A-Z]/.test(v) && /[a-z]/.test(v) && /\d/.test(v), "passwordStrength");

export const businessStepSchema = z.object({
  businessName: z.string().trim().min(2, "businessName").max(255, "businessName"),
  businessType: z.enum(BUSINESS_TYPES),
  country: z.string().regex(/^[A-Z]{2}$/, "required"),
});

export const accountStepSchema = z
  .object({
    fullName: z.string().trim().min(2, "fullName").max(200, "fullName"),
    email: z.string().trim().email("email"),
    password: passwordSchema,
    confirmPassword: z.string(),
  })
  .refine((v) => v.password === v.confirmPassword, {
    path: ["confirmPassword"],
    message: "passwordMismatch",
  });

export const termsSchema = z.object({
  terms: z.literal(true, { errorMap: () => ({ message: "terms" }) }),
});

export type BusinessStep = z.infer<typeof businessStepSchema>;
export type AccountStep = z.infer<typeof accountStepSchema>;
