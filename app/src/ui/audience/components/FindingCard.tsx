import { useState } from "react";
import { ArrowRight, ChevronDown } from "lucide-react";
import type { Finding, ViewId } from "../lib/insights";
import { SeverityChip } from "./widgets";

/**
 * A finding is a question with an answer, not a row in a table.
 *
 * The question is the heading because that is how the reader arrived: they
 * wanted to know something. The evidence list exists so the number can be
 * defended — a governance claim nobody can check is worthless in a review.
 */
export function FindingCard({
  finding,
  onNavigate,
}: {
  finding: Finding;
  onNavigate: (v: ViewId) => void;
}) {
  const [open, setOpen] = useState(false);
  const hasEvidence = finding.evidence.length > 0;

  return (
    <article className={`finding finding-${finding.severity}`}>
      <div className="finding-main">
        <div className="finding-top">
          <p className="finding-question">{finding.question}</p>
          <SeverityChip severity={finding.severity} />
        </div>

        <h3 className="finding-headline">{finding.headline}</h3>
        <p className="finding-detail">{finding.detail}</p>

        <div className="finding-actions">
          {hasEvidence && (
            <button
              className="btn btn-ghost"
              onClick={() => setOpen((o) => !o)}
              aria-expanded={open}
            >
              <ChevronDown
                size={14}
                strokeWidth={2.2}
                style={{
                  transform: open ? "rotate(180deg)" : "none",
                  transition: "transform .15s ease",
                }}
              />
              {open ? "Hide evidence" : "Show evidence"}
            </button>
          )}
          {finding.goTo && (
            <button
              className="btn btn-ghost"
              onClick={() => onNavigate(finding.goTo!)}
            >
              Investigate
              <ArrowRight size={14} strokeWidth={2.2} />
            </button>
          )}
        </div>

        {open && hasEvidence && (
          <ul className="evidence">
            {finding.evidence.map((e) => (
              <li key={e}>{e}</li>
            ))}
            {finding.count > finding.evidence.length && (
              <li className="evidence-more">
                and {finding.count - finding.evidence.length} more
              </li>
            )}
          </ul>
        )}
      </div>
    </article>
  );
}
