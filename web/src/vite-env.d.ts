/// <reference types="vite/client" />

interface ImportMetaEnv {
  /** 운영 빌드에서 백엔드 절대경로(예: https://api.example.com). 미설정 시 상대경로. */
  readonly VITE_API_BASE?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
