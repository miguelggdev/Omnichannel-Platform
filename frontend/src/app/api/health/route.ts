import { NextResponse } from "next/server";

/** Healthcheck del contenedor (docker-compose y Traefik). */
export function GET() {
  return NextResponse.json({ status: "ok" });
}
