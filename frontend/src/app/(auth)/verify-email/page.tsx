import { getTranslations } from "next-intl/server";
import { VerifyEmail } from "@/components/auth/VerifyEmail";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";

export const dynamic = "force-dynamic";

export async function generateMetadata() {
  const t = await getTranslations("verifyEmail");
  return { title: t("title") };
}

export default async function VerifyEmailPage({
  searchParams,
}: {
  searchParams: Promise<{ token?: string }>;
}) {
  const { token } = await searchParams;
  const t = await getTranslations("verifyEmail");
  return (
    <Card>
      <CardHeader className="text-center">
        <CardTitle className="text-2xl">{t("title")}</CardTitle>
      </CardHeader>
      <CardContent>
        <VerifyEmail token={token ?? null} />
      </CardContent>
    </Card>
  );
}
