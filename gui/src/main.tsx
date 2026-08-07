import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

import { App } from "./App";
import { Boundary } from "./Boundary";
import { note, reportEverythingUncaught } from "./log";
import "./style.css";

// Before anything is rendered: a crash during the first render would otherwise
// have nowhere to go, and a window with nowhere to write things down is a
// window nobody can fix.
reportEverythingUncaught();

const root = document.getElementById("root");
if (!root) throw new Error("index.html has no #root");

note("info", "окно открылось");

createRoot(root).render(
  <StrictMode>
    <Boundary>
      <App />
    </Boundary>
  </StrictMode>,
);
