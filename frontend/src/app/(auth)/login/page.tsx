import { MessageSquare } from "lucide-react";
import Link from "next/link";
import { getTranslations } from "next-intl/server";
import { LoginForm } from "@/components/auth/LoginForm";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";

// El enlace al registro depende de `ONBOARDING_ENABLED`, que se lee en cada peticion.
export const dynamic = "force-dynamic";

export async function generateMetadata() {
  const t = await getTranslations("auth");
  return { title: t("signIn") };
}

export default async function LoginPage() {
  const t = await getTranslations("auth");
  return (
    <Card>
      <CardHeader className="items-center text-center">
        <div className="mb-2 flex h-12 w-12 items-center justify-center rounded-xl bg-primary">
          <MessageSquare className="h-6 w-6 text-primary-foreground" aria-hidden />
        </div>
        <CardTitle className="text-2xl">{t("welcome")}</CardTitle>
        <CardDescription>{t("subtitle")}</CardDescription>
      </CardHeader>
      <CardContent>
        <LoginForm />
        {process.env.ONBOARDING_ENABLED === "true" && (
          <p className="mt-4 text-center text-sm text-muted-foreground">
            {t("noAccount")}{" "}
            <Link
              href="/onboarding"
              className="font-medium text-primary underline-offset-4 hover:underline"
            >
              {t("createAccount")}
            </Link>
          </p>
        )}
      </CardContent>
    </Card>
  );
}
