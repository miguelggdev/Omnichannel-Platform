"use client";

import { BookOpen, MessageSquare, UserRoundCog, Users } from "lucide-react";
import { useTranslations } from "next-intl";
import Link from "next/link";
import { PageHeader } from "@/components/common/PageHeader";
import { StatCard } from "@/components/dashboard/StatCard";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { useAppLocale } from "@/hooks/useAppLocale";
import { useDashboardStats } from "@/hooks/useDashboard";
import { formatNumber } from "@/lib/format";

export default function DashboardPage() {
  const t = useTranslations("dashboard");
  const locale = useAppLocale();
  const { esperando, abiertas, contactos, documentos } = useDashboardStats();
  const fmt = (n: number | undefined) => (n === undefined ? undefined : formatNumber(n, locale));

  return (
    <>
      <PageHeader title={t("title")} description={t("subtitle")} />
      <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
        <StatCard label={t("waitingHuman")} value={fmt(esperando.data)} icon={UserRoundCog} hint={t("waitingHumanHint")} />
        <StatCard label={t("openConversations")} value={fmt(abiertas.data)} icon={MessageSquare} />
        <StatCard label={t("contacts")} value={fmt(contactos.data)} icon={Users} />
        <StatCard label={t("knowledgeDocs")} value={fmt(documentos.data)} icon={BookOpen} />
      </div>

      <Card className="mt-6">
        <CardHeader>
          <CardTitle>{t("quickStart")}</CardTitle>
          <CardDescription>{t("quickStartHint")}</CardDescription>
        </CardHeader>
        <CardContent className="flex flex-wrap gap-2">
          <Button asChild>
            <Link href="/conversations">{t("goConversations")}</Link>
          </Button>
          <Button variant="outline" asChild>
            <Link href="/documents">{t("goDocuments")}</Link>
          </Button>
          <Button variant="outline" asChild>
            <Link href="/settings/preferences">{t("goPreferences")}</Link>
          </Button>
        </CardContent>
      </Card>
    </>
  );
}
