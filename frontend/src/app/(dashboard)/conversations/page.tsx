import { getTranslations } from "next-intl/server";
import { ConversationList } from "@/components/conversations/ConversationList";
import { PageHeader } from "@/components/common/PageHeader";

export async function generateMetadata() {
  const t = await getTranslations("conversations");
  return { title: t("title") };
}

export default async function ConversationsPage() {
  const t = await getTranslations("conversations");
  return (
    <>
      <PageHeader title={t("title")} description={t("subtitle")} />
      <ConversationList />
    </>
  );
}
