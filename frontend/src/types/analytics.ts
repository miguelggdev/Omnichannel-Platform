export interface DashboardMetrics {
  active_conversations: number;
  waiting_human: number;
  conversations_last_7_days: number;
  conversations_trend: number | null;
  messages_last_24h: number;
  messages_trend: number | null;
  token_usage_percentage: number | null;
  avg_response_time_seconds: number | null;
  median_response_time_seconds: number | null;
  csat_score: number | null;
  csat_responses: number;
  total_contacts: number;
}

export interface ChannelCount {
  channel: string;
  count: number;
}

export interface DailyMessages {
  /** Fecha ISO (YYYY-MM-DD, UTC). */
  date: string;
  inbound: number;
  outbound: number;
}
