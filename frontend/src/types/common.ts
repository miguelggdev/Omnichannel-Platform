/** Pagina que devuelven todos los listados del backend. */
export interface Paginated<T> {
  items: T[];
  total: number;
  page: number;
  page_size: number;
}

/** Forma del error que devuelve la API (`AppException`). */
export interface ApiError {
  error_code: string;
  message: string;
}
