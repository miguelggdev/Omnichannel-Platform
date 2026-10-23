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

/** El refresh token no va en el cuerpo: lo guarda el navegador en una cookie httpOnly. */
export interface TokenResponse {
  access_token: string;
  token_type: string;
}
