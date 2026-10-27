import { Lock } from "lucide-react";

/**
 * What a locked page shows once the trial (or a paid period) has ended: what
 * happened, that nothing is lost, and the one way forward. The overview pages
 * and Settings stay open, so the organisation can still see what it has.
 */
export function Locked({
  page,
  admin,
  trial,
  onChoosePlan,
}: {
  page: string;
  admin: boolean;
  /** Whether what ended was the trial, rather than a paid plan. */
  trial: boolean;
  onChoosePlan: () => void;
}) {
  return (
    <div className="locked" role="region" aria-label={`${page} is locked`}>
      <span className="locked-icon">
        <Lock size={22} strokeWidth={2} aria-hidden="true" />
      </span>
      <h1 className="locked-title">{page} is part of a paid plan</h1>
      <p className="locked-text">
        {trial
          ? "Your 7-day trial has ended, and the tenant connection and everything collected from it have been deleted. Choose a plan, then reconnect your tenant and scan to start again."
          : "Your plan has ended. The Docs overview and the Report Audience overview stay open, and everything collected is kept — choose a plan to open the rest again."}
      </p>
      {admin ? (
        <button type="button" className="btn btn-primary" onClick={onChoosePlan}>
          Choose a plan
        </button>
      ) : (
        <p className="muted-note">Ask one of your organisation's admins to choose a plan.</p>
      )}
    </div>
  );
}
