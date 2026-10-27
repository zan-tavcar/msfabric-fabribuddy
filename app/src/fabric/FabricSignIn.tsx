// Fabric sign-in, around the whole app.
//
// Opened from the Fabric workspace, the app runs inside Fabric's page and the
// session is handed over silently (initEmbeddedAuth): nobody sees this
// component. Opened directly by its URL, there is no host to hand it over, so
// one button opens Fabric in a new tab to confirm the account — the browser
// only allows that from a click.
import { useEffect, useState, type ReactNode } from "react";
import {
  ensureSignedInWithFabric,
  initEmbeddedAuth,
} from "@microsoft/rayfin-auth-provider-fabric";
import { Loader2 } from "lucide-react";

import { fabricAuthOptions, getClient } from "./client";

type State = "checking" | "signed-in" | "needs-click" | "signing-in";

export function FabricSignIn({ children }: { children: ReactNode }) {
  const [state, setState] = useState<State>("checking");
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    const auth = getClient().auth;
    (async () => {
      try {
        if (auth.getSession()?.isAuthenticated) {
          if (!cancelled) setState("signed-in");
          return;
        }
        const session = await initEmbeddedAuth(auth, fabricAuthOptions());
        if (!cancelled) setState(session?.isAuthenticated ? "signed-in" : "needs-click");
      } catch (e) {
        if (!cancelled) {
          setError(e instanceof Error ? e.message : String(e));
          setState("needs-click");
        }
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  const signIn = () => {
    setState("signing-in");
    setError(null);
    // Called inside the click: the SDK may open a Fabric tab.
    ensureSignedInWithFabric(getClient().auth, fabricAuthOptions())
      .then(() => setState("signed-in"))
      .catch((e: unknown) => {
        setError(e instanceof Error ? e.message : String(e));
        setState("needs-click");
      });
  };

  if (state === "signed-in") return <>{children}</>;
  if (state === "checking") {
    return (
      <div className="boot">
        <Loader2 className="spin" size={20} aria-hidden="true" />
        <span>Loading FabriBuddy…</span>
      </div>
    );
  }
  return (
    <div className="boot">
      <div className="stack-sm" style={{ maxWidth: 380, textAlign: "center" }}>
        <strong>Open FabriBuddy with your Fabric account</strong>
        <span className="muted-note">
          Opened from your Fabric workspace this happens on its own. Here, Fabric opens in a
          new tab once to confirm who you are.
        </span>
        <button
          type="button"
          className="btn btn-primary"
          onClick={signIn}
          disabled={state === "signing-in"}
        >
          {state === "signing-in" ? "Waiting for Fabric…" : "Continue with Fabric"}
        </button>
        {error && <span className="banner banner-danger">{error}</span>}
      </div>
    </div>
  );
}
