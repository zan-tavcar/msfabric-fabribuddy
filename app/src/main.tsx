// The Fabric App's entry: the SaaS UI (src/ui, unchanged), reading from the
// Fabric SQL database instead of a backend, behind Fabric sign-in.
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

import App from "./ui/App";
import { setTransport } from "./ui/api/client";
import { FabricSignIn } from "./fabric/FabricSignIn";
import { fabricTransport, localTransport } from "./fabric/transport";
import "./ui/styles/tokens.css";
import "./ui/styles/app.css";

const local = import.meta.env.MODE === "preview";
setTransport(local ? localTransport() : fabricTransport);

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    {local ? (
      <App />
    ) : (
      <FabricSignIn>
        <App />
      </FabricSignIn>
    )}
  </StrictMode>,
);
