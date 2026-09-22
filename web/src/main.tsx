// Lenovmail — authored by satuapps (satuapps.com)
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import App from "./App";
import "@fontsource-variable/inter/wght.css";
import "@fontsource-variable/jetbrains-mono/wght.css";
import "./styles.css";

const container = document.getElementById("root");
if (container === null) throw new Error("#root not found in index.html");
createRoot(container).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
