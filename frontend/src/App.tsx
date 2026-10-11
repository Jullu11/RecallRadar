import { useEffect, useMemo, useState } from "react";
import type { DashboardPayload, MatchRow, Product, RecoveryCase } from "./types";

type View = "home" | "desk";

function decisionLabel(decision: string): string {
  if (decision === "affected") return "Affected (historical match)";
  if (decision === "possible_match") return "Possible match";
  if (decision === "no_match") return "No match in checked sources";
  return decision;
}

function productTitle(product: Product): string {
  const brand = product.brand?.trim();
  return brand ? `${brand} — ${product.product_name}` : product.product_name;
}

export default function App() {
  const [view, setView] = useState<View>("home");
  const [data, setData] = useState<DashboardPayload | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [selectedProductId, setSelectedProductId] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    if (view !== "desk") return;
    let cancelled = false;
    const load = async () => {
      setLoading(true);
      setError(null);
      try {
        const response = await fetch("/api/dashboard");
        if (!response.ok) throw new Error(`Dashboard request failed (${response.status})`);
        const payload = (await response.json()) as DashboardPayload;
        if (cancelled) return;
        setData(payload);
        setSelectedProductId((current) => current ?? payload.products[0]?.id ?? null);
      } catch (err) {
        if (!cancelled) setError(err instanceof Error ? err.message : "Failed to load dashboard");
      } finally {
        if (!cancelled) setLoading(false);
      }
    };
    void load();
    const timer = window.setInterval(() => void load(), 8000);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, [view]);

  const selectedProduct = useMemo(
    () => data?.products.find((item) => item.id === selectedProductId) ?? null,
    [data, selectedProductId],
  );

  const selectedMatches = useMemo(() => {
    if (!data || !selectedProductId) return [] as MatchRow[];
    return data.matches.filter((item) => item.product_id === selectedProductId);
  }, [data, selectedProductId]);

  const selectedFollowUps = useMemo(() => {
    if (!data || !selectedProductId) return [];
    return data.follow_ups.filter((item) => item.product_id === selectedProductId);
  }, [data, selectedProductId]);

  const selectedRecoveryCases = useMemo(() => {
    if (!data || !selectedProductId) return [] as RecoveryCase[];
    return data.recovery_cases.filter((item) => item.product_id === selectedProductId);
  }, [data, selectedProductId]);

  const openRecoveryCases = useMemo(() => {
    if (!data) return [] as RecoveryCase[];
    return data.recovery_cases.filter((item) => item.status === "open");
  }, [data]);

  const deadlineLabel = (dueDate: string) => {
    const today = new Date();
    today.setHours(0, 0, 0, 0);
    const due = new Date(`${dueDate}T00:00:00`);
    const days = Math.round((due.getTime() - today.getTime()) / 86_400_000);
    if (days < 0) return `Overdue by ${Math.abs(days)} day${days === -1 ? "" : "s"}`;
    if (days === 0) return "Due today";
    if (days === 1) return "Due tomorrow";
    return `Due in ${days} days`;
  };

  if (view === "home") {
    return (
      <div className="page">
        <section className="hero" aria-label="Lodestone home">
          <div className="hero-media" aria-hidden="true" />
          <div className="hero-top">
            <div className="brand">Lodestone</div>
            <div className="live-dot">Recall checks in preview</div>
          </div>
          <div className="hero-copy">
            <p className="eyebrow">Every purchase, on record.</p>
            <h1>Save the receipt once. Put it to work later.</h1>
            <p>
              Keep proof of what you bought for warranty claims, recalls, returns, price refunds, and other ways
              to recover money. Lodestone starts with product recall checks; more purchase recovery tools are
              planned.
            </p>
            <div className="cta-row">
              <button className="btn btn-primary" type="button" onClick={() => setView("desk")}>
                Open evidence desk
              </button>
              <a className="btn btn-ghost" href="sms:+16282647754&body=HELP">
                Text the agent HELP
              </a>
            </div>
          </div>
        </section>
      </div>
    );
  }

  return (
    <div className="page">
      <main className="desk">
        <header className="desk-header">
          <div>
            <p className="eyebrow">Lodestone</p>
            <h2>Recall evidence desk</h2>
          </div>
          <div className="desk-meta">
            <div>{data ? `Updated ${new Date(data.generated_at).toLocaleString()}` : "Loading…"}</div>
            <div>{data ? `Persistence: ${data.persistence}` : null}</div>
            <button className="btn btn-ghost" type="button" style={{ color: "var(--ink)", marginTop: "0.5rem" }} onClick={() => setView("home")}>
              Back to home
            </button>
          </div>
        </header>

        {data ? (
          <section className="notice-band" aria-label="Historical demo notice">
            <div>
              <p className="eyebrow">Historical fixture</p>
              <h3>{data.demo_notice.title}</h3>
              <p>
                CPSC recall {data.demo_notice.recall_number} ({data.demo_notice.recall_date}). This is labeled
                historical demonstration data, not a claim of a current active recall.
              </p>
              <a href={data.demo_notice.url} target="_blank" rel="noreferrer">
                Open official CPSC notice
              </a>
            </div>
            <div>
              <p className="eyebrow">Stated remedy</p>
              <p>{data.demo_notice.remedy}</p>
              <p className="eyebrow" style={{ marginTop: "1rem" }}>
                Hazard
              </p>
              <p>{data.demo_notice.hazard}</p>
            </div>
          </section>
        ) : null}

        {error ? <p className="error">{error}</p> : null}
        {loading && !data ? <p className="empty">Loading inventory from the live agent…</p> : null}

        <section className="reminder-panel" aria-label="Open purchase reminders">
          <div className="reminder-heading">
            <div>
              <p className="eyebrow">Purchase recovery</p>
              <h3 className="panel-title">Open deadline reminders</h3>
            </div>
            <span>{openRecoveryCases.length} open</span>
          </div>
          {!openRecoveryCases.length ? (
            <p className="empty">No open dates tracked. Text RETURN BY, WARRANTY UNTIL, or PRICE ADJUST BY with a date, then use REMINDERS to review what is due.</p>
          ) : (
            <ul className="reminder-list">
              {openRecoveryCases.map((item) => {
                const product = data?.products.find((candidate) => candidate.id === item.product_id);
                return (
                  <li key={item.id}>
                    <button type="button" onClick={() => setSelectedProductId(item.product_id)}>
                      <strong>{product ? productTitle(product) : "Saved product"}</strong>
                      <span>
                        {item.case_type.replace("_", " ")} · {deadlineLabel(item.due_date)} · {item.due_date}
                      </span>
                    </button>
                  </li>
                );
              })}
            </ul>
          )}
          <p className="reminder-note">Dates come from you. Lodestone does not verify retailer terms or send proactive messages yet.</p>
        </section>

        <div className="desk-grid">
          <section>
            <h3 className="panel-title">Inventory</h3>
            {!data?.products.length ? (
              <p className="empty">No saved products yet. Confirm an item over iMessage to see it here.</p>
            ) : (
              <ul className="product-list">
                {data.products.map((product, index) => (
                  <li key={product.id}>
                    <button
                      type="button"
                      className={`product-item${selectedProductId === product.id ? " active" : ""}`}
                      style={{ animationDelay: `${index * 60}ms` }}
                      onClick={() => setSelectedProductId(product.id)}
                    >
                      <strong>{productTitle(product)}</strong>
                      <span>
                        {product.status}
                        {product.monitoring_enabled ? " · monitoring on" : " · monitoring off"}
                      </span>
                    </button>
                  </li>
                ))}
              </ul>
            )}
          </section>

          <section className="detail" aria-live="polite">
            {!selectedProduct ? (
              <p className="empty">Select a product to inspect match evidence.</p>
            ) : (
              <>
                <h3>{productTitle(selectedProduct)}</h3>
                <dl>
                  <div>
                    <dt>Identifiers</dt>
                    <dd>
                      {(() => {
                        const parts = Object.entries(selectedProduct.identifiers || {})
                          .filter(([, value]) => Boolean(value))
                          .map(([key, value]) => `${key}: ${value}`);
                        return parts.length ? parts.join(" · ") : "None confirmed yet";
                      })()}
                    </dd>
                  </div>
                  <div>
                    <dt>Capture details</dt>
                    <dd>
                      {(() => {
                        const fields = [
                          ["Brand", selectedProduct.brand ? "brand" : ""],
                          ["Product", "product_name"],
                          ...Object.keys(selectedProduct.identifiers || {}).map((key) => [key.replaceAll("_", " "), key]),
                        ] as const;
                        const details = fields.flatMap(([label, key]) => {
                          const origin = selectedProduct.field_provenance?.[key];
                          if (!origin) return [];
                          const source = origin.user_corrected
                            ? "corrected by you"
                            : origin.source === "user"
                              ? "entered by you"
                              : `read from ${origin.source_type || "photo"}`;
                          return [`${label}: ${source}`];
                        });
                        return details.length ? details.join(" · ") : "Capture source details unavailable for this record";
                      })()}
                    </dd>
                  </div>
                  <div>
                    <dt>Purchase recovery</dt>
                    <dd>
                      {!selectedRecoveryCases.length ? (
                        <span>
                          No return, warranty, or price-adjustment dates tracked. Text HELP to the Lodestone agent
                          to see how to add one.
                        </span>
                      ) : (
                        <ul className="evidence-list">
                          {selectedRecoveryCases.map((item) => (
                            <li key={item.id} className="evidence-item" style={{ cursor: "default" }}>
                              <strong>{item.case_type.replace("_", " ")}</strong>
                              <span>
                                {item.case_type === "return"
                                  ? "Return by"
                                  : item.case_type === "warranty"
                                    ? "Coverage ends"
                                    : "Request by"}{" "}
                                {item.due_date} · {item.status.replace("_", " ")}
                                {item.date_source === "user_confirmed" ? " · date provided by you" : ""}
                              </span>
                              {item.status === "open" ? (
                                <span>
                                  Draft a merchant message: text PACKET {((data?.recovery_cases.findIndex((action) => action.id === item.id) ?? -1) + 1)} FOR &lt;merchant&gt;
                                </span>
                              ) : null}
                            </li>
                          ))}
                        </ul>
                      )}
                    </dd>
                  </div>
                  <div>
                    <dt>Latest checks</dt>
                    <dd>
                      {!selectedMatches.length ? (
                        <span>No completed match records yet.</span>
                      ) : (
                        <ul className="evidence-list">
                          {selectedMatches.map((match) => (
                            <li key={match.id} className="evidence-item" style={{ cursor: "default" }}>
                              <strong className={`status ${match.decision}`}>{decisionLabel(match.decision)}</strong>
                              <span>
                                Checked {new Date(match.checked_at).toLocaleString()}
                                {typeof match.evidence.summary === "string" ? ` · ${match.evidence.summary}` : ""}
                              </span>
                              {typeof match.evidence.official_url === "string" ? (
                                <div>
                                  <a href={match.evidence.official_url} target="_blank" rel="noreferrer">
                                    Source notice
                                  </a>
                                </div>
                              ) : null}
                            </li>
                          ))}
                        </ul>
                      )}
                    </dd>
                  </div>
                  <div>
                    <dt>Follow-up</dt>
                    <dd>
                      {!selectedFollowUps.length
                        ? "No follow-up recorded."
                        : selectedFollowUps
                            .map((item) =>
                              item.completed_at
                                ? `${item.action} (completed)`
                                : `${item.action} (open)`,
                            )
                            .join(" · ")}
                    </dd>
                  </div>
                </dl>
              </>
            )}
          </section>
        </div>
      </main>
    </div>
  );
}
