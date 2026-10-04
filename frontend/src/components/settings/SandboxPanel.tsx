"use client";

import axios from "axios";
import { FlaskConical, Loader2, RotateCcw, Send, Upload, Undo2 } from "lucide-react";
import { useTranslations } from "next-intl";
import { useState } from "react";
import { toast } from "sonner";
import { ConfirmDialog } from "@/components/common/ConfirmDialog";
import { QueryError } from "@/components/common/QueryError";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";
import { useAppLocale } from "@/hooks/useAppLocale";
import { useSandboxActions, useSandboxStatus } from "@/hooks/useSandbox";
import { apiPost, errorMessage } from "@/lib/api";
import { formatDateTime } from "@/lib/format";

interface Turno {
  rol: "user" | "bot";
  texto: string;
  detalle?: string;
}

type Accion = "reset" | "publish" | "rollback";

function SandboxChat() {
  const t = useTranslations("sandbox");
  const [texto, setTexto] = useState("");
  const [turnos, setTurnos] = useState<Turno[]>([]);
  const [enviando, setEnviando] = useState(false);

  const enviar = async () => {
    const mensaje = texto.trim();
    if (!mensaje || enviando) return;
    setTurnos((p) => [...p, { rol: "user", texto: mensaje }]);
    setTexto("");
    setEnviando(true);
    try {
      const r = await apiPost<{
        response: string | null;
        intent: string | null;
        requires_handoff: boolean;
        detected_language: string | null;
      }>("/sandbox/messages", { text: mensaje, new_conversation: turnos.length === 0 });
      const detalle = [r.intent && `intent: ${r.intent}`, r.detected_language && `lang: ${r.detected_language}`, r.requires_handoff && t("handoff")]
        .filter(Boolean)
        .join(" · ");
      setTurnos((p) => [...p, { rol: "bot", texto: r.response ?? t("noResponse"), detalle }]);
    } catch (e) {
      toast.error(errorMessage(e));
    } finally {
      setEnviando(false);
    }
  };

  return (
    <Card>
      <CardHeader>
        <CardTitle>{t("tryTitle")}</CardTitle>
        <CardDescription>{t("tryHint")}</CardDescription>
      </CardHeader>
      <CardContent className="space-y-3">
        <div className="max-h-72 space-y-2 overflow-y-auto rounded-md border p-3" role="log" aria-live="polite">
          {turnos.length === 0 && <p className="text-sm text-muted-foreground">{t("tryEmpty")}</p>}
          {turnos.map((m, i) => (
            <div key={i} className={m.rol === "user" ? "text-end" : "text-start"}>
              <span
                className={
                  m.rol === "user"
                    ? "inline-block max-w-[85%] whitespace-pre-wrap rounded-2xl bg-primary px-3 py-2 text-sm text-primary-foreground"
                    : "inline-block max-w-[85%] whitespace-pre-wrap rounded-2xl bg-muted px-3 py-2 text-sm"
                }
              >
                {m.texto}
              </span>
              {m.detalle && <p className="mt-0.5 text-xs text-muted-foreground">{m.detalle}</p>}
            </div>
          ))}
        </div>
        <form
          className="flex gap-2"
          onSubmit={(e) => {
            e.preventDefault();
            void enviar();
          }}
        >
          <Input
            value={texto}
            onChange={(e) => setTexto(e.target.value)}
            placeholder={t("tryPlaceholder")}
            aria-label={t("tryPlaceholder")}
            maxLength={4000}
          />
          <Button type="submit" disabled={enviando || !texto.trim()} aria-label={t("send")}>
            {enviando ? <Loader2 className="h-4 w-4 animate-spin" aria-hidden /> : <Send className="h-4 w-4" aria-hidden />}
          </Button>
        </form>
      </CardContent>
    </Card>
  );
}

export function SandboxPanel() {
  const t = useTranslations("sandbox");
  const locale = useAppLocale();
  const { data, isLoading, error, refetch } = useSandboxStatus();
  const actions = useSandboxActions();
  const [confirmar, setConfirmar] = useState<Accion | null>(null);

  // 403: la flag `enable_sandbox` no esta activa para este tenant.
  if (axios.isAxiosError(error) && error.response?.status === 403) {
    return (
      <Card>
        <CardContent className="flex items-start gap-3 p-6">
          <FlaskConical className="mt-0.5 h-5 w-5 shrink-0 text-muted-foreground" aria-hidden />
          <div>
            <p className="font-medium">{t("disabledTitle")}</p>
            <p className="text-sm text-muted-foreground">{t("disabledBody")}</p>
          </div>
        </CardContent>
      </Card>
    );
  }
  if (error) return <QueryError error={error} onRetry={() => void refetch()} />;
  if (isLoading || !data) return <Skeleton className="h-48 w-full" />;

  if (!data.exists) {
    return (
      <Card>
        <CardHeader>
          <CardTitle>{t("noneTitle")}</CardTitle>
          <CardDescription>{t("noneBody")}</CardDescription>
        </CardHeader>
        <CardContent>
          <Button
            onClick={() =>
              actions.create.mutate(undefined, {
                onSuccess: () => toast.success(t("created")),
                onError: (e) => toast.error(errorMessage(e)),
              })
            }
            disabled={actions.create.isPending}
          >
            {actions.create.isPending && <Loader2 className="h-4 w-4 animate-spin" aria-hidden />}
            {t("create")}
          </Button>
        </CardContent>
      </Card>
    );
  }

  const ejecutar = (accion: Accion) => {
    const m = actions[accion];
    m.mutate(undefined as never, {
      onSuccess: () => {
        toast.success(t(`done.${accion}`));
        setConfirmar(null);
      },
      onError: (e: unknown) => toast.error(errorMessage(e)),
    });
  };

  return (
    <div className="space-y-6">
      <Card>
        <CardHeader>
          <CardTitle>{t("statusTitle")}</CardTitle>
          <CardDescription>
            {t("lastReset", { when: formatDateTime(data.reset_at ?? data.created_at, locale) })}
            {data.last_published_at && ` · ${t("lastPublished", { when: formatDateTime(data.last_published_at, locale) })}`}
          </CardDescription>
        </CardHeader>
        <CardContent className="flex flex-wrap gap-2">
          <Button variant="outline" onClick={() => setConfirmar("reset")}>
            <RotateCcw className="h-4 w-4" aria-hidden />
            {t("reset")}
          </Button>
          <Button onClick={() => setConfirmar("publish")}>
            <Upload className="h-4 w-4" aria-hidden />
            {t("publish")}
          </Button>
          <Button variant="outline" disabled={data.versions === 0} onClick={() => setConfirmar("rollback")}>
            <Undo2 className="h-4 w-4" aria-hidden />
            {t("rollback", { versions: data.versions })}
          </Button>
        </CardContent>
      </Card>

      <SandboxChat />

      <ConfirmDialog
        open={confirmar !== null}
        onOpenChange={(open) => !open && setConfirmar(null)}
        title={confirmar ? t(`confirm.${confirmar}.title`) : ""}
        description={confirmar ? t(`confirm.${confirmar}.body`) : ""}
        confirmLabel={confirmar ? t(`confirm.${confirmar}.action`) : ""}
        destructive={confirmar !== "reset" ? confirmar === "rollback" : false}
        pending={confirmar ? actions[confirmar].isPending : false}
        onConfirm={() => confirmar && ejecutar(confirmar)}
      />
    </div>
  );
}
