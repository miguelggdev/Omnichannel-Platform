export const CONVERSATION_STATUSES = [
  "new",
  "bot_active",
  "human_active",
  "waiting_human",
  "waiting_client",
  "resolved",
  "archived",
] as const;
export type ConversationStatus = (typeof CONVERSATION_STATUSES)[number];

export const CHANNELS = [
  "whatsapp",
  "instagram",
  "facebook",
  "telegram",
  "email",
  "webchat",
  "voice",
  "sandbox",
] as const;
export type Channel = (typeof CHANNELS)[number];

export interface Conversation {
  id: string;
  client_id: string;
  contact_id: string;
  channel: string;
  status: ConversationStatus;
  assigned_user_id: string | null;
  subject: string | null;
  last_message_at: string | null;
  resolved_at: string | null;
  created_at: string;
  updated_at: string;
}

export interface Message {
  id: string;
  direction: "inbound" | "outbound";
  message_type: string;
  content: string | null;
  media_url: string | null;
  sender_type: string;
  sender_id: string | null;
  created_at: string;
}

export interface ConversationDetail extends Conversation {
  messages: Message[];
  total_messages: number;
  page: number;
  page_size: number;
}

/** Transiciones validas del ciclo de vida; espejo de `VALID_TRANSITIONS` del backend. */
export const STATUS_TRANSITIONS: Record<ConversationStatus, readonly ConversationStatus[]> = {
  new: ["bot_active", "human_active"],
  bot_active: ["human_active", "waiting_human", "resolved"],
  human_active: ["waiting_client", "resolved"],
  waiting_human: ["human_active", "resolved"],
  waiting_client: ["human_active", "bot_active", "resolved"],
  resolved: ["archived"],
  archived: [],
};
