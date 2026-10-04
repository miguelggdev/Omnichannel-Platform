export const BUSINESS_TYPES = [
  "restaurant",
  "clinic",
  "ecommerce",
  "services",
  "education",
  "other",
] as const;
export type BusinessType = (typeof BUSINESS_TYPES)[number];

/** Cuerpo de `POST /onboarding/register` (espejo de `OnboardingRequest`). */
export interface OnboardingRequest {
  business_name: string;
  business_type: BusinessType;
  admin_full_name: string;
  admin_email: string;
  admin_password: string;
  country: string;
  language: string;
  terms_accepted: boolean;
}

export interface OnboardingResponse {
  client_id: string;
  user_id: string;
  access_token: string;
  refresh_token: string;
  token_type: string;
}
