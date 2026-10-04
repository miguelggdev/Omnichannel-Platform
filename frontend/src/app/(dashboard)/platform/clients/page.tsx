"use client";

import { useTranslations } from "next-intl";
import { ProtectedRoute } from "@/components/auth/ProtectedRoute";
import { PageHeader } from "@/components/common/PageHeader";
import { ClientList } from "@/components/platform/ClientList";

export default function ClientsPage() {
  const t = useTranslations("platform.clients");
  return (
    <ProtectedRoute minRole="super_admin">
      <PageHeader title={t("title")} description={t("subtitle")} />
      <ClientList />
    </ProtectedRoute>
  );
}
