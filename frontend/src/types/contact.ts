export interface Contact {
  id: string;
  client_id: string;
  first_name: string | null;
  last_name: string | null;
  display_name: string | null;
  merged_into_id: string | null;
  created_at: string;
  updated_at: string;
}

export interface ContactIdentifier {
  id: string;
  channel: string;
  identifier_value: string;
  created_at: string;
}

export interface ContactTag {
  id: string;
  name: string;
  color: string | null;
}

export interface ContactNote {
  id: string;
  author_id: string;
  content: string;
  created_at: string;
}

export interface ContactDetail extends Contact {
  metadata: Record<string, unknown>;
  identifiers: ContactIdentifier[];
  tags: ContactTag[];
  notes: ContactNote[];
}
