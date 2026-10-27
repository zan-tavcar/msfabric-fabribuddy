import { type FormEvent, useEffect, useState } from "react";
import { Loader2, MailCheck } from "lucide-react";
import { ApiError, api } from "../api/client";
import type { AuthConfig, Me } from "../api/types";
import { Brand } from "../components/Brand";
import { PasswordRules, passwordOk } from "../components/PasswordRules";

// "sent": after sign-up, waiting for the person to open the confirmation link.
// "microsoft": back from a Microsoft sign-up, naming the organisation.
type Mode =
  | "login"
  | "register"
  | "forgot"
  | "reset"
  | "verify"
  | "invite"
  | "sent"
  | "microsoft";

/**
 * Sign in, create an account, and the e-mail link flows (verify, reset).
 *
 * The page a signed-out visitor sees, and nothing else: every data route is
 * behind a session, so there is no half-loaded app behind this screen.
 */
export function AuthScreen({
  config,
  onSignedIn,
}: {
  config: AuthConfig;
  onSignedIn: (me: Me) => void;
}) {
  const params = new URLSearchParams(window.location.search);
  const [mode, setMode] = useState<Mode>(
    params.get("ms_signup")
      ? "microsoft"
      : params.get("invite")
        ? "invite"
        : params.get("reset")
          ? "reset"
          : params.get("verify")
            ? "verify"
            : // The website's "Start the trial" links here with ?signup.
              params.has("signup")
              ? "register"
              : "login",
  );
  const [invitation, setInvitation] = useState<{ email: string; role: string } | null>(null);
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [name, setName] = useState("");
  const [organisation, setOrganisation] = useState("");
  const [busy, setBusy] = useState(false);
  // Microsoft sent the person back with a reason it could not sign them in.
  const [error, setError] = useState<string | null>(params.get("ms_error"));
  const [notice, setNotice] = useState<string | null>(null);
  // The account waiting for its confirmation link, and when it may ask again.
  const [sentTo, setSentTo] = useState("");
  const [unverified, setUnverified] = useState(false);
  const [resendAt, setResendAt] = useState(0);
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (resendAt <= now) return;
    const timer = setTimeout(() => setNow(Date.now()), 1000);
    return () => clearTimeout(timer);
  }, [resendAt, now]);
  const wait = Math.max(0, Math.ceil((resendAt - now) / 1000));

  const showSent = (address: string) => {
    setSentTo(address);
    setPassword("");
    setResendAt(Date.now() + 30_000);
    setNow(Date.now());
    setMode("sent");
  };

  const resend = async () => {
    setBusy(true);
    setError(null);
    try {
      await api.resendVerification(sentTo || email);
      showSent(sentTo || email);
      setNotice("Sent again. It can take a minute to arrive.");
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  const clearQuery = () => window.history.replaceState(null, "", window.location.pathname);

  const continueWithMicrosoft = async () => {
    setBusy(true);
    setError(null);
    try {
      window.location.assign((await api.microsoftStart()).url);
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e));
      setBusy(false);
    }
  };

  // Back from Microsoft: an error to show, or a new person to finish signing up.
  useEffect(() => {
    if (params.get("ms_error")) clearQuery();
    const token = params.get("ms_signup");
    if (mode !== "microsoft" || !token) return;
    api
      .microsoftSignup(token)
      .then((who) => {
        setEmail(who.email);
        setName(who.name);
        setOrganisation(who.organisation);
      })
      .catch((e) => {
        clearQuery();
        setError(e instanceof ApiError ? e.message : String(e));
        setMode("register");
      });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // An e-mail verification link signs the user straight in.
  useEffect(() => {
    const token = params.get("verify");
    if (mode !== "verify" || !token) return;
    api
      .verify(token)
      .then((r) => {
        clearQuery();
        onSignedIn(r.user);
      })
      .catch((e) => {
        setError(e instanceof ApiError ? e.message : String(e));
        setMode("login");
      });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // An invitation link: show who it is for before asking for a password.
  useEffect(() => {
    const token = params.get("invite");
    if (mode !== "invite" || !token) return;
    api
      .invitation(token)
      .then(setInvitation)
      .catch((e) => {
        clearQuery();
        setError(e instanceof ApiError ? e.message : String(e));
        setMode("login");
      });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    setBusy(true);
    setError(null);
    setNotice(null);
    setUnverified(false);
    try {
      if (mode === "login") {
        onSignedIn((await api.login(email, password)).user);
      } else if (mode === "register") {
        const result = await api.register(email, password, name, organisation);
        if (result.user) onSignedIn(result.user);
        else {
          showSent(email);
        }
      } else if (mode === "microsoft") {
        const result = await api.microsoftRegister(params.get("ms_signup") ?? "", organisation);
        clearQuery();
        onSignedIn(result.user);
      } else if (mode === "invite") {
        const result = await api.acceptInvitation(params.get("invite") ?? "", password, name);
        clearQuery();
        onSignedIn(result.user);
      } else if (mode === "forgot") {
        await api.forgot(email);
        setNotice("If that address has an account, a reset link is on its way.");
        setMode("login");
      } else if (mode === "reset") {
        await api.reset(params.get("reset") ?? "", password);
        clearQuery();
        setNotice("Password changed. Sign in with the new one.");
        setPassword("");
        setMode("login");
      }
    } catch (e) {
      // 403 at sign-in: the password is right, the address not confirmed yet.
      if (mode === "login" && e instanceof ApiError && e.status === 403) setUnverified(true);
      setError(e instanceof ApiError ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  const title = {
    login: "Sign in",
    register: "Create your account",
    forgot: "Reset your password",
    reset: "Choose a new password",
    verify: "Confirming your e-mail…",
    invite: "Join your team",
    sent: "Check your inbox",
    microsoft: "Name your organisation",
  }[mode];

  return (
    <div className="auth-shell">
      <form className="auth-card" onSubmit={submit} noValidate>
        <Brand />
        {config.registrationOpen && (mode === "login" || mode === "register") && (
          <div className="segmented auth-switch" role="tablist" aria-label="Sign in or sign up">
            {(
              [
                ["login", "Sign in"],
                ["register", "Create account"],
              ] as const
            ).map(([value, label]) => (
              <button
                key={value}
                type="button"
                role="tab"
                aria-selected={mode === value}
                className={mode === value ? "active" : ""}
                onClick={() => {
                  setMode(value);
                  setError(null);
                  setUnverified(false);
                }}
              >
                {label}
              </button>
            ))}
          </div>
        )}
        <h1 className="auth-title">{title}</h1>
        <p className="auth-lede">
          FabriBuddy for Microsoft Fabric and Power BI — <strong>Data Catalog</strong>: what
          writes each table and what reads it; <strong>Report Audience</strong>: who can see
          each report, who opens it, which seats nobody uses. Read-only, metadata only.
        </p>

        {notice && <p className="banner banner-ok">{notice}</p>}
        {error && (
          <p className="banner banner-danger" role="alert">
            {error}
          </p>
        )}

        {unverified && mode === "login" && (
          <button type="button" className="link auth-resend" onClick={resend} disabled={busy}>
            Send the confirmation e-mail again
          </button>
        )}

        {mode === "verify" ? (
          <Loader2 className="spin" size={20} aria-hidden="true" />
        ) : mode === "sent" ? (
          <div className="auth-sent">
            <span className="auth-sent-icon">
              <MailCheck size={22} aria-hidden="true" />
            </span>
            <p>
              We sent a confirmation link to <strong>{sentTo}</strong>. Open it to finish
              creating your account — it signs you in and starts your 7-day trial.
            </p>
            <p className="muted-note">
              The link is valid for 24 hours. Nothing there? Check your spam folder, or send
              it again.
            </p>
            <button
              type="button"
              className="btn auth-submit"
              onClick={resend}
              disabled={busy || wait > 0}
            >
              {busy && <Loader2 className="spin" size={14} aria-hidden="true" />}
              {wait > 0 ? `Send again in ${wait}s` : "Send the link again"}
            </button>
          </div>
        ) : (
          <>
            {mode === "invite" && invitation && (
              <p className="auth-invite">
                You were invited as <strong>{invitation.email}</strong> with{" "}
                {invitation.role === "admin"
                  ? "admin access — you can manage the connection, workspaces, scans and members."
                  : invitation.role === "contributor"
                    ? "contributor access — you can read everything and write table and column descriptions."
                    : "read access to your organisation's Data Catalog and Report Audience."}
              </p>
            )}
            {(mode === "login" || mode === "register") && config.microsoftSignIn && (
              <>
                <button
                  type="button"
                  className="btn auth-microsoft"
                  onClick={continueWithMicrosoft}
                  disabled={busy}
                >
                  <MicrosoftLogo />
                  Continue with Microsoft
                </button>
                <p className="auth-or">
                  <span>or with e-mail</span>
                </p>
              </>
            )}
            {mode === "microsoft" && email && (
              <p className="auth-invite">
                Signed in with Microsoft as <strong>{email}</strong>. Name your organisation to
                start the 7-day trial — you are its admin.
              </p>
            )}
            {(mode === "register" || mode === "invite") && (
              <Field label="Your name" id="a-name">
                <input
                  id="a-name"
                  className="input"
                  value={name}
                  autoComplete="name"
                  onChange={(e) => setName(e.target.value)}
                />
              </Field>
            )}
            {(mode === "register" || mode === "microsoft") && (
              <Field label="Organisation" id="a-org">
                <input
                  id="a-org"
                  className="input"
                  value={organisation}
                  autoComplete="organization"
                  placeholder="Defaults to your e-mail domain"
                  onChange={(e) => setOrganisation(e.target.value)}
                />
              </Field>
            )}
            {mode !== "reset" && mode !== "invite" && mode !== "microsoft" && (
              <Field label="Work e-mail" id="a-email">
                <input
                  id="a-email"
                  className="input"
                  type="email"
                  required
                  autoComplete="email"
                  value={email}
                  onChange={(e) => setEmail(e.target.value)}
                />
              </Field>
            )}
            {mode !== "forgot" && mode !== "microsoft" && (
              <Field
                label={mode === "reset" ? "New password" : "Password"}
                id="a-password"
              >
                <input
                  id="a-password"
                  className="input"
                  type="password"
                  required
                  autoComplete={mode === "login" ? "current-password" : "new-password"}
                  value={password}
                  onChange={(e) => setPassword(e.target.value)}
                />
                {(mode === "register" || mode === "reset" || mode === "invite") && (
                  <PasswordRules password={password} />
                )}
              </Field>
            )}
            {(mode === "register" || mode === "microsoft") && (
              <p className="muted-note auth-verify-note">
                {mode === "register" &&
                  config.emailVerification &&
                  "We'll e-mail you a link to confirm your address before you can sign in. "}
                Your 7-day trial starts then. If it ends without a plan, the tenant connection
                and everything collected are deleted.
              </p>
            )}
            <button
              type="submit"
              className="btn btn-primary auth-submit"
              disabled={
                busy ||
                ((mode === "register" || mode === "reset" || mode === "invite") &&
                  !passwordOk(password))
              }
            >
              {busy && <Loader2 className="spin" size={14} aria-hidden="true" />}
              {
                {
                  login: "Sign in",
                  register: "Create account",
                  forgot: "Send reset link",
                  reset: "Set password",
                  invite: "Create account",
                  microsoft: "Start the trial",
                }[mode as Exclude<Mode, "verify" | "sent">]
              }
            </button>
          </>
        )}

        <div className="auth-links">
          {mode === "login" && config.emailVerification && (
            <button type="button" className="link" onClick={() => setMode("forgot")}>
              Forgot password?
            </button>
          )}
          {mode !== "login" && mode !== "verify" && mode !== "register" && (
            <button type="button" className="link" onClick={() => setMode("login")}>
              Back to sign in
            </button>
          )}
        </div>
      </form>
    </div>
  );
}

/** Microsoft's four-square mark, as its sign-in button guidelines show it. */
function MicrosoftLogo() {
  return (
    <svg width="16" height="16" viewBox="0 0 21 21" aria-hidden="true">
      <rect x="1" y="1" width="9" height="9" fill="#f25022" />
      <rect x="11" y="1" width="9" height="9" fill="#7fba00" />
      <rect x="1" y="11" width="9" height="9" fill="#00a4ef" />
      <rect x="11" y="11" width="9" height="9" fill="#ffb900" />
    </svg>
  );
}

function Field({
  label,
  id,
  hint,
  children,
}: {
  label: string;
  id: string;
  hint?: string;
  children: React.ReactNode;
}) {
  return (
    <label htmlFor={id} className="form-field">
      <span className="form-label">{label}</span>
      {children}
      {hint && <span className="form-hint">{hint}</span>}
    </label>
  );
}
