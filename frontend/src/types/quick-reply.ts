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

export interface RenderedQuickReply {
  shortcut: string;
  content: string;
  /** Variables que no se pudieron resolver y quedaron como `{{nombre}}` en el texto. */
  unresolved: string[];
}
