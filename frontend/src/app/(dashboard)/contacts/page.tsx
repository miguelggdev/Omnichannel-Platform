import { getTranslations } from "next-intl/server";
import { PageHeader } from "@/components/common/PageHeader";
import { ContactList } from "@/components/contacts/ContactList";

export async function generateMetadata() {
  const t = await getTranslations("contacts");
  return { title: t("title") };
}

export default async function ContactsPage() {
  const t = await getTranslations("contacts");
  return (
    <>
      <PageHeader title={t("title")} description={t("subtitle")} />
      <ContactList />
    </>
  );
}
