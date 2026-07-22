/// <reference types="vite/client" />

interface ImportMetaEnv {
  readonly VITE_API_BASE_URL?: string;
  readonly VITE_API_TIMEOUT_MS?: string;
  readonly VITE_FORCE_REPLAY?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
