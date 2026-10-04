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
