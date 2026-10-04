export type UserRole = "super_admin" | "admin" | "supervisor" | "agent" | "medical";

export interface User {
  id: string;
  client_id: string;
  email: string;
  role: UserRole;
}

export interface LoginRequest {
  email: string;
  password: string;
}

export interface TokenResponse {
  access_token: string;
  refresh_token: string;
  token_type: string;
}
