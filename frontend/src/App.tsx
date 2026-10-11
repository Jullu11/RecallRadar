import { useEffect, useMemo, useState } from "react";
import type { DashboardPayload, MatchRow, Product, RecoveryCase, PriceWatch } from "./types";

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
  const authUrl = import.meta.env.VITE_SUPABASE_URL;
  const authAnonKey = import.meta.env.VITE_SUPABASE_ANON_KEY;
  const authConfigured = Boolean(authUrl && authAnonKey);
  const [view, setView] = useState<View>("home");
  const [data, setData] = useState<DashboardPayload | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [selectedProductId, setSelectedProductId] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [accessToken, setAccessToken] = useState(() => window.localStorage.getItem("lodestone_access_token"));
  const [email, setEmail] = useState("");
  const [otp, setOtp] = useState("");
  const [otpSent, setOtpSent] = useState(false);
  const [authError, setAuthError] = useState<string | null>(null);
  const [linkedSender, setLinkedSender] = useState<string | null | undefined>(undefined);
  const [linkCode, setLinkCode] = useState<string | null>(null);

  const sendOtp = async () => {
    setAuthError(null);
    try {
      const response = await fetch(`${authUrl}/auth/v1/otp`, {
        method: "POST",
        headers: { apikey: authAnonKey!, "content-type": "application/json" },
        body: JSON.stringify({ email, create_user: true }),
      });
      if (!response.ok) throw new Error("Could not send a sign-in code. Check the email and Supabase Auth settings.");
      setOtpSent(true);
    } catch (err) {
      setAuthError(err instanceof Error ? err.message : "Sign-in request failed.");
    }
  };

  const verifyOtp = async () => {
    setAuthError(null);
    try {
      const response = await fetch(`${authUrl}/auth/v1/verify`, {
        method: "POST",
        headers: { apikey: authAnonKey!, "content-type": "application/json" },
        body: JSON.stringify({ email, token: otp, type: "email" }),
      });
      if (!response.ok) throw new Error("That code did not verify. Check it and try again.");
      const session = (await response.json()) as { access_token?: string };
      if (!session.access_token) throw new Error("Supabase Auth returned no access token.");
      window.localStorage.setItem("lodestone_access_token", session.access_token);
      setAccessToken(session.access_token);
      setLinkedSender(undefined);
    } catch (err) {
      setAuthError(err instanceof Error ? err.message : "Code verification failed.");
    }
  };

  const requestLinkCode = async () => {
    setAuthError(null);
    try {
      const response = await fetch("/api/account/link-code", {
        method: "POST",
        headers: { Authorization: `Bearer ${accessToken}` },
      });
      const payload = (await response.json()) as { code?: string; detail?: string };
      if (!response.ok || !payload.code) throw new Error(payload.detail || "Could not create a phone link code.");
      setLinkCode(payload.code);
    } catch (err) {
      setAuthError(err instanceof Error ? err.message : "Could not create a phone link code.");
    }
  };

  useEffect(() => {
    if (view !== "desk") return;
    if (authConfigured && !accessToken) return;
    let cancelled = false;
    const load = async () => {
      setLoading(true);
      setError(null);
      try {
        const headers = accessToken ? { Authorization: `Bearer ${accessToken}` } : undefined;
        if (authConfigured) {
          const accountResponse = await fetch("/api/account", { headers });
          if (!accountResponse.ok) throw new Error("Your sign-in expired. Sign in again to continue.");
          const accountPayload = (await accountResponse.json()) as { linked: boolean; sender_id: string | null };
          if (cancelled) return;
          setLinkedSender(accountPayload.sender_id);
          if (!accountPayload.linked) {
            setData(null);
            return;
          }
        }
        const response = await fetch("/api/dashboard", { headers });
      if (!response.ok) {
        if (response.status === 401) throw new Error("Your sign-in expired. Sign in again to continue.");
        throw new Error(`Dashboard request failed (${response.status})`);
      }
        const payload = (await response.json()) as DashboardPayload;
        if (cancelled) return;
        setData(payload);
        setSelectedProductId((current) => current ?? payload.products[0]?.id ?? null);
      } catch (err) {
        if (!cancelled) {
          const message = err instanceof Error ? err.message : "Failed to load dashboard";
          setError(message);
          if (message.includes("sign-in expired")) {
            window.localStorage.removeItem("lodestone_access_token");
            setAccessToken(null);
          }
        }
      } finally {
        if (!cancelled) setLoading(false);
      }
    };
    void load();
    const timer = window.setInterval(() => void load(), 15000);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, [view, authConfigured, accessToken]);

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

  const priceWatches = useMemo(() => data?.price_watches ?? ([] as PriceWatch[]), [data]);

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

  if (authConfigured && !accessToken) {
    return (
      <div className="page">
        <main className="auth-card">
          <p className="eyebrow">Lodestone account</p>
          <h2>{otpSent ? "Check your email" : "Sign in to your evidence desk"}</h2>
          {otpSent ? (
            <>
              <p>Enter the one-time code sent to {email}.</p>
              <input className="auth-input" inputMode="numeric" autoComplete="one-time-code" value={otp} onChange={(event) => setOtp(event.target.value)} placeholder="Email code" />
              <button className="btn btn-primary" type="button" onClick={() => void verifyOtp()}>Verify code</button>
            </>
          ) : (
            <>
              <p>Use a one-time email code to view records linked to your phone.</p>
              <input className="auth-input" type="email" autoComplete="email" value={email} onChange={(event) => setEmail(event.target.value)} placeholder="you@example.com" />
              <button className="btn btn-primary" type="button" onClick={() => void sendOtp()}>Email me a code</button>
            </>
          )}
          {authError ? <p className="error">{authError}</p> : null}
          <button className="btn btn-ghost" type="button" onClick={() => setView("home")}>Back to home</button>
        </main>
      </div>
    );
  }

  if (authConfigured && accessToken && linkedSender === null) {
    return (
      <div className="page">
        <main className="auth-card">
          <p className="eyebrow">Connect your phone</p>
          <h2>Link your iMessage account</h2>
          <p>Generate a short-lived code, then text the exact <strong>LINK &lt;code&gt;</strong> shown below from the same phone number that sends your receipts.</p>
          {linkCode ? <p className="link-code">LINK {linkCode}</p> : null}
          <button className="btn btn-primary" type="button" onClick={() => void requestLinkCode()}>Generate link code</button>
          {authError ? <p className="error">{authError}</p> : null}
          <p className="reminder-note">The code expires in 10 minutes. This page will open your records after the phone is linked.</p>
          <button className="btn btn-ghost" type="button" onClick={() => { window.localStorage.removeItem("lodestone_access_token"); setAccessToken(null); }}>Sign out</button>
        </main>
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
            {authConfigured ? (
              <button className="btn btn-ghost" type="button" style={{ color: "var(--ink)", marginTop: "0.5rem" }} onClick={() => {
                window.localStorage.removeItem("lodestone_access_token");
                setAccessToken(null);
                setData(null);
              }}>
                Sign out
              </button>
            ) : null}
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

        <section className="reminder-panel" aria-label="Amazon price watches">
          <div className="reminder-heading">
            <div>
              <p className="eyebrow">Price recovery</p>
              <h3 className="panel-title">Amazon price watches</h3>
            </div>
            <span>{priceWatches.filter((watch) => watch.is_active).length} active</span>
          </div>
          {!priceWatches.length ? (
            <p className="empty">No price watches yet. Add one by texting WATCH PRICE with an item number, Amazon product link, and target price.</p>
          ) : (
            <ul className="reminder-list">
              {priceWatches.map((watch, index) => {
                const product = data?.products.find((candidate) => candidate.id === watch.product_id);
                const reached = watch.current_price !== null && watch.current_price <= watch.target_price;
                return (
                  <li key={watch.id}>
                    <div className="price-watch-row">
                      <div>
                        <strong>{watch.provider_title || (product ? productTitle(product) : `Amazon ${watch.asin}`)}</strong>
                        <span>
                          {watch.current_price === null
                            ? "Not checked yet"
                            : `Latest recorded $${watch.current_price.toFixed(2)} · target $${watch.target_price.toFixed(2)} · ${reached ? "target reached" : "target not reached"}`}
                        </span>
                        <span>{watch.is_active ? "Active" : "Paused"}{watch.last_checked_at ? ` · checked ${new Date(watch.last_checked_at).toLocaleString()}` : ""}</span>
                        {watch.check_error ? <span>{watch.check_error}</span> : null}
                      </div>
                      <div className="price-watch-actions">
                        <a href={watch.product_url} target="_blank" rel="noreferrer">Open Amazon listing</a>
                        {watch.is_active ? <span>Text PRICE CHECK {index + 1} to refresh</span> : null}
                      </div>
                    </div>
                  </li>
                );
              })}
            </ul>
          )}
          <p className="reminder-note">Amazon.com only. Prices come from Keepa’s latest recorded data and may lag. Checks are manual by default; a deployed scheduler can call the protected check endpoint. No price-drop iMessages are sent.</p>
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
