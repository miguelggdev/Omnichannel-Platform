"use client";

import { use } from "react";
import { ProtectedRoute } from "@/components/auth/ProtectedRoute";
import { ClientDetail } from "@/components/platform/ClientDetail";

export default function ClientPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = use(params);
  return (
    <ProtectedRoute minRole="super_admin">
      <ClientDetail id={id} />
    </ProtectedRoute>
  );
}
