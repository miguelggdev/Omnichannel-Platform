export interface QuickReply {
  id: string;
  shortcut: string;
  title: string;
  content: string;
  category: string | null;
  created_by: string | null;
  created_at: string;
}

export interface QuickReplyInput {
  shortcut: string;
  title: string;
  content: string;
  category?: string | null;
}
