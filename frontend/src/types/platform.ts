export interface ClientSummary {
  id: string;
  name: string;
  slug: string;
  plan: string;
  is_active: boolean;
  created_at: string;
  suspended_at: string | null;
  users_count: number;
  conversations_30d: number;
  messages_30d: number;
  last_message_at: string | null;
}

export interface ClientDetail {
  id: string;
  name: string;
  slug: string;
  plan: string;
  is_active: boolean;
  created_at: string;
  suspended_at: string | null;
  alert_message: string | null;
  users_total: number;
  users_active: number;
  contacts_total: number;
  conversations_open: number;
  conversations_30d: number;
  messages_30d: number;
  last_message_at: string | null;
  documents_ready: number;
  has_agent: boolean;
  token_used: number | null;
  token_budget: number | null;
}

export interface WorkerInfo {
  hostname: string;
  pid: number | null;
  concurrency: number | null;
  active_tasks: number;
  reserved_tasks: number;
  processed_total: number | null;
  queues: string[];
  uptime_seconds: number | null;
}

export interface WorkersResponse {
  /** `false`: no se pudo hablar con el broker; no se sabe cuantos workers hay. */
  broker_ok: boolean;
  workers: WorkerInfo[];
}

export interface QueueDepth {
  name: string;
  pending: number;
}

export interface TaskInfo {
  id: string;
  name: string;
  worker: string;
  state: "active" | "reserved" | "scheduled";
  queue: string | null;
  started_at: number | null;
}

export interface RedisInfo {
  version: string;
  uptime_seconds: number;
  connected_clients: number;
  used_memory: number;
  /** 0 = sin limite configurado. */
  max_memory: number;
  total_keys: number;
  hit_ratio: number | null;
}

export interface ComponentStatus {
  name: string;
  ok: boolean;
  latency_ms: number | null;
  detail: string | null;
}

export interface SystemStatus {
  version: string;
  environment: string;
  components: ComponentStatus[];
}

export type SecurityStatus = "ok" | "warn" | "fail";

export interface SecurityCheck {
  id: string;
  status: SecurityStatus;
  detail: string | null;
}

export interface RlsTableStatus {
  name: string;
  rls_enabled: boolean;
  rls_forced: boolean;
}

export interface SecurityOverview {
  environment: string;
  checks: SecurityCheck[];
  rls_tables: RlsTableStatus[];
  rls_unprotected: string[];
}
