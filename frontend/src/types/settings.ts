import type { UserRole } from "./auth";
export type UiLanguage = "es" | "en" | "pt" | "it" | "de" | "fr";
export type ThemePreference = "light" | "dark" | "system";

export interface UserPreferences {
  ui_language: UiLanguage;
  theme: ThemePreference;
}

export interface FeatureFlagItem {
  flag: string;
  value: boolean | number | null;
  enforced: boolean;
  editable: boolean;
}

export interface SandboxStatus {
  exists: boolean;
  sandbox_client_id: string | null;
  created_at: string | null;
  reset_at: string | null;
  last_published_at: string | null;
  versions: number;
}

/** Un usuario del equipo, tal como lo devuelve `GET /admin/users` (sin contrasena). */
export interface TeamUser {
  id: string;
  client_id: string;
  email: string;
  first_name: string;
  last_name: string;
  role: UserRole;
  is_active: boolean;
  last_login_at: string | null;
}

/** Roles que un administrador puede asignar; `super_admin` es de la plataforma. */
export const ASSIGNABLE_ROLES = ["admin", "supervisor", "agent", "medical"] as const;
export type AssignableRole = (typeof ASSIGNABLE_ROLES)[number];
