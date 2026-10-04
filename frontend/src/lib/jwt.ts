import type { User, UserRole } from "@/types/auth";

const ROLES: readonly UserRole[] = ["super_admin", "admin", "supervisor", "agent", "medical"];

function decodeBase64Url(segmento: string): string {
  const base64 = segmento.replace(/-/g, "+").replace(/_/g, "/");
  const relleno = base64.padEnd(base64.length + ((4 - (base64.length % 4)) % 4), "=");
  const binario = atob(relleno);
  const bytes = Uint8Array.from(binario, (c) => c.charCodeAt(0));
  return new TextDecoder().decode(bytes);
}

/**
 * Lee el usuario de los claims del access token.
 *
 * Solo sirve para pintar la interfaz (menu segun rol, nombre): no verifica la firma, y el
 * backend vuelve a comprobar el rol en cada peticion. Devuelve `null` si el token no tiene la
 * forma esperada.
 */
export function userFromToken(token: string): User | null {
  try {
    const payload = token.split(".")[1];
    if (!payload) return null;
    const claims = JSON.parse(decodeBase64Url(payload)) as Record<string, unknown>;
    const { user_id, client_id, email, role } = claims;
    if (
      typeof user_id !== "string" ||
      typeof client_id !== "string" ||
      typeof email !== "string" ||
      typeof role !== "string" ||
      !ROLES.includes(role as UserRole)
    ) {
      return null;
    }
    return { id: user_id, client_id, email, role: role as UserRole };
  } catch {
    return null;
  }
}
