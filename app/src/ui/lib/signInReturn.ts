/**
 * Coming back from Microsoft after linking it to the account:
 * `/?settings=account&ms=linked` or `&ms_error=…`.
 *
 * Read once when the app loads, then cleared from the address bar, so a reload
 * does not repeat the message — and read here rather than in a component,
 * because a component may render (and so read) more than once.
 */
function read() {
  const params = new URLSearchParams(window.location.search);
  if (params.get("settings") !== "account") return null;
  const outcome = {
    ok:
      params.get("ms") === "linked"
        ? "Microsoft is linked: you can now sign in with it."
        : undefined,
    error: params.get("ms_error") ?? undefined,
  };
  try {
    window.history.replaceState(null, "", window.location.pathname);
  } catch {
    // the outcome still shows; the URL just keeps its query
  }
  return outcome;
}

export const signInReturn: { ok?: string; error?: string } | null = read();
