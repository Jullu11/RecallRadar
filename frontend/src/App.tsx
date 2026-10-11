import { useEffect, useMemo, useState } from "react";
import type { DashboardPayload, MatchRow, Product } from "./types";

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
