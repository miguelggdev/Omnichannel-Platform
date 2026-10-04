"use client";

import { useTranslations } from "next-intl";
import { Bar, BarChart, Cell, Legend, Line, LineChart, Pie, PieChart, ResponsiveContainer, Tooltip, XAxis, YAxis, CartesianGrid } from "recharts";
import { useAppLocale } from "@/hooks/useAppLocale";
import { formatNumber } from "@/lib/format";
import type { ChannelCount, DailyMessages, QueueDepth } from "@/types";

/**
 * Paleta de canales. El color acompana, no informa: cada porcion lleva su nombre en la leyenda
 * y en el tooltip, asi que no depende de distinguir tonos (accesibilidad).
 */
const COLORES: Record<string, string> = {
  whatsapp: "#25D366",
  instagram: "#E4405F",
  facebook: "#1877F2",
  telegram: "#26A5E4",
  email: "#6B7280",
  webchat: "#6366F1",
  voice: "#8B5CF6",
  sandbox: "#F59E0B",
};
const GRIS = "#9CA3AF";

export function ChannelPie({ data }: { data: ChannelCount[] }) {
  const t = useTranslations("conversations");
  const locale = useAppLocale();
  const filas = data.map((d) => ({
    name: ["whatsapp", "instagram", "facebook", "telegram", "email", "webchat", "voice", "sandbox"].includes(d.channel)
      ? t(`channel.${d.channel}` as "channel.whatsapp")
      : d.channel,
    value: d.count,
    fill: COLORES[d.channel] ?? GRIS,
  }));

  return (
    <ResponsiveContainer width="100%" height={260}>
      <PieChart>
        <Pie data={filas} dataKey="value" nameKey="name" innerRadius={55} outerRadius={90} paddingAngle={2}>
          {filas.map((f) => (
            <Cell key={f.name} fill={f.fill} />
          ))}
        </Pie>
        <Tooltip formatter={(v: number) => formatNumber(v, locale)} />
        <Legend />
      </PieChart>
    </ResponsiveContainer>
  );
}

export function MessagesLine({ data }: { data: DailyMessages[] }) {
  const t = useTranslations("dashboard");
  const locale = useAppLocale();
  // "2026-10-04" -> "4 oct": la fecha es de un dia civil UTC, sin hora, asi que se formatea a
  // mano con Date.UTC para que la zona horaria del navegador no la corra un dia.
  const dia = (iso: string) =>
    new Intl.DateTimeFormat(locale, { day: "numeric", month: "short", timeZone: "UTC" }).format(new Date(`${iso}T00:00:00Z`));

  return (
    <ResponsiveContainer width="100%" height={260}>
      <LineChart data={data} margin={{ top: 8, right: 12, left: -12, bottom: 0 }}>
        <CartesianGrid strokeDasharray="3 3" className="stroke-border" />
        <XAxis dataKey="date" tickFormatter={dia} fontSize={12} />
        <YAxis allowDecimals={false} fontSize={12} />
        <Tooltip labelFormatter={dia} formatter={(v: number) => formatNumber(v, locale)} />
        <Legend />
        <Line type="monotone" dataKey="inbound" name={t("inbound")} stroke="#3B82F6" strokeWidth={2} dot={{ r: 3 }} />
        <Line type="monotone" dataKey="outbound" name={t("outbound")} stroke="#10B981" strokeWidth={2} strokeDasharray="5 3" dot={{ r: 3 }} />
      </LineChart>
    </ResponsiveContainer>
  );
}

/** Mensajes esperando en cada cola de Celery. */
export function QueueBars({ data }: { data: QueueDepth[] }) {
  const t = useTranslations("platform.celery");
  return (
    <ResponsiveContainer width="100%" height={240}>
      <BarChart data={data} margin={{ top: 8, right: 12, left: -12, bottom: 0 }}>
        <CartesianGrid strokeDasharray="3 3" className="stroke-border" />
        <XAxis dataKey="name" fontSize={12} interval={0} angle={-20} textAnchor="end" height={50} />
        <YAxis allowDecimals={false} fontSize={12} />
        <Tooltip />
        <Bar dataKey="pending" name={t("pending")} fill="#3B82F6" radius={[4, 4, 0, 0]} />
      </BarChart>
    </ResponsiveContainer>
  );
}
