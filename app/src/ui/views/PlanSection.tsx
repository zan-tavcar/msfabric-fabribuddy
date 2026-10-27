import { useEffect, useState } from "react";
import { Check, Loader2 } from "lucide-react";
import { ApiError, api } from "../api/client";
import type { PlanCatalogue, PlanCode, Subscription } from "../api/types";
import { Card, Chip, SectionHead } from "../components/primitives";
import { money, num } from "../lib/format";
import { openPaddleCheckout } from "../lib/paddle";

/** Short, customer-facing label for the subscription's state. */
export function planStatusLabel(sub: Subscription): string {
  if (!sub.active) return "Ended";
  if (sub.status === "trialing") {
    return sub.trialDaysLeft === null ? "Trial" : `Trial · ${sub.trialDaysLeft} days left`;
  }
  if (sub.status === "past_due") return "Payment overdue";
  return sub.daysLeft === null ? "Active" : `Renews in ${sub.daysLeft} days`;
}

function workspacesLabel(cap: number | null): string {
  return cap === null ? "Unlimited workspaces" : `Up to ${cap} workspaces`;
}

/**
 * The organisation's one subscription: both products, priced by the number of
 * workspaces scanned (Team 10, Business 40, Enterprise unlimited).
 *
 * With Stripe configured, choosing a plan goes to Stripe Checkout (or, for a
 * customer already paying, Stripe's portal) and the plan turns on when Stripe's
 * signed webhook says so; without it, the choice applies directly.
 */
export function PlanSection({
  subscription,
  onChanged,
}: {
  subscription: Subscription | null;
  onChanged: (next: Subscription) => void;
}) {
  const [catalogue, setCatalogue] = useState<PlanCatalogue | null>(null);
  const [busy, setBusy] = useState<PlanCode | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  // Paddle's overlay reported the payment done; the webhook activates the plan.
  const [paid, setPaid] = useState(false);

  /** Re-read the subscription a few times: the provider's webhook usually lands in seconds. */
  const watchForChange = () => {
    const before = subscription?.planCode;
    let tries = 0;
    const timer = setInterval(async () => {
      tries += 1;
      const next = await api.subscription().catch(() => null);
      if (next) onChanged(next);
      if ((next && next.planCode !== before && next.active) || tries >= 10) {
        clearInterval(timer);
        setNotice(null);
      }
    }, 3000);
  };
  useEffect(() => {
    if (paid) watchForChange();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [paid]);

  useEffect(() => {
    api
      .plans()
      .then(setCatalogue)
      .catch((e) => setError(e instanceof ApiError ? e.message : String(e)));
  }, []);

  // Back from Stripe Checkout. Only the webhook activates the plan, so this
  // says what is happening rather than claiming it is done.
  const [returned] = useState(() => new URLSearchParams(window.location.search).get("billing"));
  useEffect(() => {
    if (!returned) return;
    window.history.replaceState(null, "", window.location.pathname);
    if (returned !== "success") return;
    // The webhook usually lands within seconds; re-read a few times.
    let tries = 0;
    const timer = setInterval(async () => {
      tries += 1;
      const next = await api.subscription().catch(() => null);
      if (next) onChanged(next);
      if ((next && next.planCode !== "trial" && next.active) || tries >= 10) clearInterval(timer);
    }, 3000);
    return () => clearInterval(timer);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const choose = async (code: PlanCode) => {
    setBusy(code);
    setError(null);
    try {
      const result = await api.choosePlan(code);
      if ("checkoutUrl" in result) {
        window.location.assign(result.checkoutUrl);
        return;
      }
      if ("paddleCheckout" in result) {
        await openPaddleCheckout(result.paddleCheckout, () => setPaid(true));
        return;
      }
      if ("requested" in result) {
        // No payment provider: the operator was told and sets the plan up.
        setNotice(
          `Thanks — we've received your request for ${result.plan}. We'll e-mail you to set up billing; you can also write to ${result.contact}.`,
        );
        return;
      }
      if ("pending" in result) {
        // A paying customer's plan change: Paddle confirms it by webhook.
        setNotice("Plan change sent to Paddle. This page updates when Paddle confirms it.");
        watchForChange();
        return;
      }
      onChanged(result);
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e));
    } finally {
      setBusy(null);
    }
  };

  const manageBilling = async () => {
    setError(null);
    try {
      window.location.assign((await api.billingPortal()).url);
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e));
    }
  };

  if (!subscription) return null;
  const sub = subscription;
  const pending = returned === "success" && !(sub.planCode !== "trial" && sub.active);
  const cap = sub.limits.workspaces;
  const used = sub.usage.workspaces;
  const share = cap ? Math.min(1, used / cap) : 0;
  const paying = sub.planCode !== "trial" && sub.active;

  return (
    <div className="stack">
      <Card>
        <div className="card-body stack-sm">
          <SectionHead
            title={`${sub.planName} plan`}
            sub={`${sub.organisation} · Data Catalog and Report Audience`}
            right={
              <Chip tone={!sub.active ? "danger" : paying ? "green" : "amber"}>
                {planStatusLabel(sub)}
              </Chip>
            }
          />
          {returned === "success" && pending && (
            <p className="banner banner-ok">
              Payment received — activating your subscription. This page updates by itself.
            </p>
          )}
          {returned === "cancelled" && (
            <p className="banner banner-warn">Checkout was cancelled; nothing was charged.</p>
          )}
          <div className="usage-grid">
            <div className="usage">
              <div className="usage-label">Workspaces scanned</div>
              <div className="usage-value tnum">
                {num(used)}
                <span className="muted-note">
                  {" "}
                  / {cap === null ? "unlimited" : num(cap)}
                </span>
              </div>
              {cap !== null && (
                <div
                  className="usage-bar"
                  role="meter"
                  aria-valuemin={0}
                  aria-valuemax={cap}
                  aria-valuenow={used}
                  aria-label="Workspaces scanned against the plan"
                >
                  <span
                    className={used > cap ? "danger" : share >= 0.8 ? "warn" : ""}
                    style={{ width: `${Math.round(share * 100)}%` }}
                  />
                </div>
              )}
            </div>
            <div className="usage">
              <div className="usage-label">Users</div>
              <div className="usage-value tnum">
                {num(sub.usage.users)} <span className="muted-note">· unlimited</span>
              </div>
              {sub.usage.invitations > 0 && (
                <div className="muted-note">{sub.usage.invitations} invitation(s) pending</div>
              )}
            </div>
            <div className="usage">
              <div className="usage-label">Scheduled scans</div>
              <div className="usage-value tnum">up to {sub.limits.dailyScans} a day</div>
            </div>
          </div>
          {sub.billingProvider === "paddle" && (
            <p className="muted-note">
              Payments are handled by Paddle, our reseller: it charges VAT or sales tax where it
              applies and sends the invoices.
            </p>
          )}
          {sub.billingEnabled && sub.hasBillingAccount && (
            <div>
              <button type="button" className="btn btn-sm" onClick={manageBilling}>
                Manage billing
              </button>
            </div>
          )}
        </div>
      </Card>

      {error && <p className="banner banner-danger">{error}</p>}
      {notice && <p className="banner banner-ok">{notice}</p>}
      {paid && (
        <p className="banner banner-ok">
          Payment received — activating your plan. This page updates by itself.
        </p>
      )}
      {!catalogue ? (
        <p className="muted-note">Loading…</p>
      ) : (
        <>
          <div className="plan-grid">
            {catalogue.plans.map((plan) => {
              const current = sub.planCode === plan.code && sub.active;
              const fits = !current && used > 0 && sub.suggestedPlan === plan.code;
              const tooSmall = plan.maxWorkspaces !== null && used > plan.maxWorkspaces;
              return (
                <div
                  key={plan.code}
                  className={`plan-card ${current || fits ? "plan-card-highlight" : ""}`}
                >
                  <div className="toolbar" style={{ justifyContent: "space-between" }}>
                    <div className="plan-name">{plan.name}</div>
                    {current ? (
                      <Chip tone="green">Current plan</Chip>
                    ) : fits ? (
                      <Chip tone="amber">Fits your {used} workspaces</Chip>
                    ) : null}
                  </div>
                  <div className="plan-price tnum">
                    {money(plan.priceMonth, catalogue.currency)}
                    <span className="plan-per">/month, excl. VAT</span>
                  </div>
                  <div className="plan-cap">{workspacesLabel(plan.maxWorkspaces)}</div>
                  <p className="muted-note">{plan.blurb}</p>
                  <button
                    type="button"
                    className={current ? "btn btn-sm" : "btn btn-sm btn-primary"}
                    disabled={current || busy !== null}
                    title={
                      tooSmall
                        ? `You scan ${used} workspaces; this plan scans the first ${plan.maxWorkspaces}.`
                        : undefined
                    }
                    onClick={() => choose(plan.code)}
                  >
                    {busy === plan.code && <Loader2 className="spin" size={13} aria-hidden="true" />}
                    {current ? "Your plan" : paying ? `Switch to ${plan.name}` : `Choose ${plan.name}`}
                  </button>
                </div>
              );
            })}
          </div>

          <Card>
            <div className="card-body stack-sm">
              <SectionHead
                title="Included in every plan"
                sub={`The ${catalogue.trial.days}-day trial is the same, on up to ${catalogue.trial.maxWorkspaces} workspaces. A workspace counts once, whichever products read it.`}
              />
              <div className="plan-includes">
                {catalogue.products.map((p) => (
                  <div key={p.product}>
                    <div className="plan-includes-title">{p.name}</div>
                    <ul className="plan-features">
                      {p.included.map((line) => (
                        <li key={line}>
                          <Check size={13} strokeWidth={2.4} aria-hidden="true" />
                          {line}
                        </li>
                      ))}
                    </ul>
                  </div>
                ))}
                <div>
                  <div className="plan-includes-title">Both</div>
                  <ul className="plan-features">
                    {catalogue.common.map((line) => (
                      <li key={line}>
                        <Check size={13} strokeWidth={2.4} aria-hidden="true" />
                        {line}
                      </li>
                    ))}
                  </ul>
                </div>
              </div>
            </div>
          </Card>
        </>
      )}
    </div>
  );
}
