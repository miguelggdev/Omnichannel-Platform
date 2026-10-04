"use client";

import { zodResolver } from "@hookform/resolvers/zod";
import { Loader2 } from "lucide-react";
import { useTranslations } from "next-intl";
import { useForm } from "react-hook-form";
import { toast } from "sonner";
import { z } from "zod";
import { Button } from "@/components/ui/button";
import { DialogFooter } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { errorMessage } from "@/lib/api";
import type { ContactInput } from "@/hooks/useContacts";

// Espejo de ContactCreate: 100 caracteres de nombre y apellido, 200 de nombre visible.
const schema = z.object({
  first_name: z.string().max(100),
  last_name: z.string().max(100),
  display_name: z.string().max(200),
});
type Values = z.infer<typeof schema>;

interface ContactFormProps {
  initial?: ContactInput;
  onSubmit: (data: ContactInput) => Promise<unknown>;
  onDone: () => void;
}

export function ContactForm({ initial, onSubmit, onDone }: ContactFormProps) {
  const t = useTranslations("contacts");
  const tc = useTranslations("common");
  const {
    register,
    handleSubmit,
    formState: { errors, isSubmitting },
  } = useForm<Values>({
    resolver: zodResolver(schema),
    defaultValues: {
      first_name: initial?.first_name ?? "",
      last_name: initial?.last_name ?? "",
      display_name: initial?.display_name ?? "",
    },
  });

  const submit = handleSubmit(async (values) => {
    try {
      // Un campo vacio se manda como null para no guardar cadenas vacias.
      await onSubmit({
        first_name: values.first_name.trim() || null,
        last_name: values.last_name.trim() || null,
        display_name: values.display_name.trim() || null,
      });
      toast.success(tc("saved"));
      onDone();
    } catch (e) {
      toast.error(errorMessage(e));
    }
  });

  return (
    <form onSubmit={submit} className="space-y-4" noValidate>
      {(["first_name", "last_name", "display_name"] as const).map((campo) => (
        <div key={campo} className="space-y-2">
          <Label htmlFor={campo}>{t(`fields.${campo}`)}</Label>
          <Input id={campo} aria-invalid={errors[campo] ? true : undefined} {...register(campo)} />
          {errors[campo] && (
            <p role="alert" className="text-sm text-destructive">
              {tc("tooLong")}
            </p>
          )}
        </div>
      ))}
      <DialogFooter>
        <Button type="submit" disabled={isSubmitting}>
          {isSubmitting && <Loader2 className="h-4 w-4 animate-spin" aria-hidden />}
          {tc("save")}
        </Button>
      </DialogFooter>
    </form>
  );
}
