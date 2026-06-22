import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// 개발 서버는 백엔드(FastAPI, 127.0.0.1:8000)로 API 를 프록시한다 — 같은 출처로 보이게
// 해 CORS 설정 없이 동작. 운영 빌드는 VITE_API_BASE 로 절대경로를 주입할 수 있다.
const API_TARGET = process.env.VITE_API_TARGET ?? "http://127.0.0.1:8000";

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/queue": API_TARGET,
      "/export": API_TARGET,
      "/health": API_TARGET,
    },
  },
});
