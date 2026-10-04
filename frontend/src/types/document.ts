export type DocumentStatus = "pending" | "processing" | "completed" | "failed";

export interface KbDocument {
  id: string;
  client_id: string;
  title: string;
  file_url: string | null;
  file_type: string | null;
  file_size: number | null;
  chunk_count: number;
  status: DocumentStatus;
  created_at: string;
  updated_at: string;
}

/** Tipos que acepta `POST /documents` (espejo de `ALLOWED_TYPES`) y su tope de 50 MB. */
export const ACCEPTED_DOCUMENT_TYPES: Record<string, string[]> = {
  "application/pdf": [".pdf"],
  "application/vnd.openxmlformats-officedocument.wordprocessingml.document": [".docx"],
  "text/plain": [".txt"],
  "text/csv": [".csv"],
  "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": [".xlsx"],
  "image/png": [".png"],
  "image/jpeg": [".jpg", ".jpeg"],
  "image/webp": [".webp"],
};
export const MAX_DOCUMENT_BYTES = 50 * 1024 * 1024;
