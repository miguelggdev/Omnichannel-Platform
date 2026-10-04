"use client";

import { useTranslations } from "next-intl";
import { PageHeader } from "@/components/common/PageHeader";
import { QuickReplyManager } from "@/components/settings/QuickReplyManager";

export default function QuickRepliesPage() {
  const t = useTranslations("quickReplies");
  return (
    <>
      <PageHeader title={t("title")} description={t("subtitle")} />
      <QuickReplyManager />
    </>
  );
}
