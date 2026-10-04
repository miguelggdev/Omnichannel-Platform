import { MessageSquare } from "lucide-react";
import { notFound } from "next/navigation";
import { getTranslations } from "next-intl/server";
import { OnboardingWizard } from "@/components/auth/OnboardingWizard";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";

// Se lee `ONBOARDING_ENABLED` en cada peticion, no al compilar.
export const dynamic = "force-dynamic";

export async function generateMetadata() {
  const t = await getTranslations("onboarding");
  return { title: t("title") };
}

export default async function OnboardingPage() {
  if (process.env.ONBOARDING_ENABLED !== "true") notFound();
  const t = await getTranslations("onboarding");
  return (
    <Card>
      <CardHeader className="items-center text-center">
        <div className="mb-2 flex h-12 w-12 items-center justify-center rounded-xl bg-primary">
          <MessageSquare className="h-6 w-6 text-primary-foreground" aria-hidden />
        </div>
        <CardTitle className="text-2xl">{t("title")}</CardTitle>
        <CardDescription>{t("subtitle")}</CardDescription>
      </CardHeader>
      <CardContent>
        <OnboardingWizard />
      </CardContent>
    </Card>
  );
}
