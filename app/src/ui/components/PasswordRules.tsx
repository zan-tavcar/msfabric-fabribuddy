import { Check, X } from "lucide-react";

/** The rules the server enforces (app/auth.py check_password_policy), in order. */
export const PASSWORD_RULES: { label: string; ok: (p: string) => boolean }[] = [
  { label: "At least 10 characters", ok: (p) => p.length >= 10 },
  { label: "A lower-case letter", ok: (p) => /[a-z]/.test(p) },
  { label: "An upper-case letter", ok: (p) => /[A-Z]/.test(p) },
  { label: "A digit", ok: (p) => /\d/.test(p) },
  { label: "A special character (!?#-_)", ok: (p) => /[^A-Za-z0-9]/.test(p) },
];

export const passwordOk = (password: string) => PASSWORD_RULES.every((r) => r.ok(password));

/** Ticks each rule off as the password is typed. */
export function PasswordRules({ password }: { password: string }) {
  return (
    <ul className="password-rules" aria-label="Password requirements">
      {PASSWORD_RULES.map((rule) => {
        const ok = rule.ok(password);
        return (
          <li key={rule.label} className={ok ? "ok" : ""}>
            {ok ? (
              <Check size={13} strokeWidth={2.6} aria-label="Done" />
            ) : (
              <X size={13} strokeWidth={2.6} aria-label="Not yet" />
            )}
            {rule.label}
          </li>
        );
      })}
    </ul>
  );
}
