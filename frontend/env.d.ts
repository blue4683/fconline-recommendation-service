/// <reference types="vite/client" />

interface ImportMetaEnv {
  /** 실제 연결 모드의 API 기준 주소. 비어 있으면 실제 연결 모드를 선택할 수 없다. */
  readonly VITE_FRS_API_BASE_URL?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
