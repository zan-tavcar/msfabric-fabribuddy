/**
 * FabriBuddy, with the open product under the wordmark (Data Catalog or
 * Report Audience).
 * The FabriBuddy mark — a magnifier in green over a small bar chart — and the
 * wordmark in two tones, "fabri" in ink and "buddy" in green, as in the logo.
 */
export function Brand({
  product = "lineage",
}: {
  product?: "lineage" | "audience" | "console";
}) {
  const name = {
    lineage: "Data Catalog",
    audience: "Report Audience",
    console: "Console",
  }[product];
  return (
    <div className="brand" aria-label={`FabriBuddy ${name}`} role="img">
      <BrandMark size={30} />
      <span className="brand-text">
        <span className="brand-name">
          <span className="ink">fabri</span>
          <span className="accent">buddy</span>
        </span>
        <span className="brand-product">{name}</span>
      </span>
    </div>
  );
}

/** The magnifier on its own (favicon, small spaces). */
export function BrandMark({ size = 30 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 64 64" aria-hidden="true">
      <circle cx="27" cy="27" r="18" fill="none" stroke="#408870" strokeWidth="7" />
      <line x1="40.5" y1="40.5" x2="56" y2="56" stroke="#408870" strokeWidth="8" strokeLinecap="round" />
      <rect x="17.5" y="29" width="5" height="7" rx="1.2" fill="#14231e" />
      <rect x="24.5" y="24" width="5" height="12" rx="1.2" fill="#14231e" />
      <rect x="31.5" y="18" width="5" height="18" rx="1.2" fill="#408870" />
    </svg>
  );
}
