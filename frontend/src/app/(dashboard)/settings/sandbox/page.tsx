"use client";

import { useTranslations } from "next-intl";
import { ProtectedRoute } from "@/components/auth/ProtectedRoute";
import { PageHeader } from "@/components/common/PageHeader";
import { FeatureFlagsPanel } from "@/components/settings/FeatureFlagsPanel";
import { SandboxPanel } from "@/components/settings/SandboxPanel";

export default function SandboxPage() {
  const t = useTranslations("sandbox");
  return (
    <ProtectedRoute minRole="admin">
      <PageHeader title={t("title")} description={t("subtitle")} />
      <div className="space-y-6">
        <SandboxPanel />
        <FeatureFlagsPanel />
      </div>
    </ProtectedRoute>
  );
}
