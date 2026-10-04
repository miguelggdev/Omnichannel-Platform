"use client";

import { useTranslations } from "next-intl";
import { ProtectedRoute } from "@/components/auth/ProtectedRoute";
import { PageHeader } from "@/components/common/PageHeader";
import { BusinessProfileForm } from "@/components/settings/BusinessProfileForm";

export default function BusinessProfilePage() {
  const t = useTranslations("businessProfile");
  return (
    <ProtectedRoute minRole="admin">
      <PageHeader title={t("title")} description={t("subtitle")} />
      <BusinessProfileForm />
    </ProtectedRoute>
  );
}
