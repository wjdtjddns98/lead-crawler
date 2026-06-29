import React from "react";
import ReactDOM from "react-dom/client";
import App from "./App";
import "./styles.css";

// `npm run dev:mock`(vite --mode mock) 일 때만 백엔드 없이 동작하는 메모리 mock 을 설치한 뒤 렌더
// (렌더 전에 admin 세션 시드·fetch 가로채기를 끝내야 App 이 로그인 화면을 건너뛰고 mock 응답을
// 받는다). MODE 로 판별하므로 별도 .env 파일이 필요 없고, 평소엔 import 자체가 일어나지 않아 운영
// 번들에 mock 코드가 포함되지 않는다.
async function boot() {
  if (import.meta.env.MODE === "mock") {
    const { installMock } = await import("./mock");
    installMock();
  }
  ReactDOM.createRoot(document.getElementById("root")!).render(
    <React.StrictMode>
      <App />
    </React.StrictMode>,
  );
}

void boot();
