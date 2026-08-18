import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { App } from "./App";
import { realtime } from "./realtime/client";
import "./styles/tokens.css";
import "./styles/app.css";

// Open the multiplexed realtime socket once at boot; hooks subscribe to topics.
realtime.connect();

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
