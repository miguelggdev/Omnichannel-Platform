export const DAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"] as const;
export type Day = (typeof DAYS)[number];

export interface DaySchedule {
  is_open: boolean;
  open_time: string | null;
  close_time: string | null;
}

export const SOCIAL_NETWORKS = ["instagram", "facebook", "twitter", "whatsapp"] as const;
export type SocialNetwork = (typeof SOCIAL_NETWORKS)[number];

/** El perfil tal como lo devuelve `GET /admin/business-profile`. */
export interface BusinessProfile {
  business_name: string;
  business_type: string | null;
  description: string | null;
  phone: string | null;
  email: string | null;
  website: string | null;
  address: string | null;
  city: string | null;
  country: string | null;
  timezone: string | null;
  social_media: Partial<Record<SocialNetwork, string>>;
  operating_hours: Record<Day, DaySchedule>;
  primary_color: string | null;
  secondary_color: string | null;
  logo_url: string | null;
  welcome_message: string | null;
  handoff_message: string | null;
  has_agent: boolean;
}

/** Cuerpo de `PUT`: solo viaja lo que cambia; `null` borra un campo. */
export type BusinessProfileUpdate = Partial<
  Omit<BusinessProfile, "social_media" | "operating_hours" | "has_agent">
> & {
  social_media?: Partial<Record<SocialNetwork, string | null>>;
  operating_hours?: Partial<Record<Day, DaySchedule>>;
};
