import { useEffect, useRef, useState, type ReactNode } from 'react';
import { createClient, type Session, type SupabaseClient } from '@supabase/supabase-js';
import './workspace.css';

type Config = { demo: boolean; supabase_url?: string; supabase_anon_key?: string; imessage_number: string; extraction_enabled: boolean; prices_enabled: boolean };
type Product = { id: string; product_name: string; brand: string | null; identifiers: Record<string, string | null>; status: string; receipt_fingerprint?: string; monitoring_enabled: boolean };
type Action = { id: string; product_id: string; case_type: string; due_date: string; status: string };
type RecordRow = { id: string; payload: Record<string, any> };
type Watch = { id: string; product_id: string; target_price: number; current_price: number | null; product_url: string; is_active: boolean; check_error?: string; last_checked_at?: string };
type Document = { fingerprint: string; filename: string; mime_type: string; created_at?: string };
type Workspace = { products: Product[]; recovery_cases: Action[]; price_watches: Watch[]; documents: Document[]; benefits: RecordRow[]; claims: RecordRow[]; recall_checks: RecordRow[]; benefit_matches: any[]; generated_at: string };
type Draft = { product_name: string; brand: string; merchant: string; purchase_date: string; amount_paid: string; payment_card: string; order_number: string; model_number: string; date_code: string; upc: string; source_fingerprint?: string; item_index: number; id?: string; identifiers?: Record<string, string | null> };
const emptyDraft = (): Draft => ({ product_name: '', brand: '', merchant: '', purchase_date: '', amount_paid: '', payment_card: '', order_number: '', model_number: '', date_code: '', upc: '', item_index: 1 });
const dollars = (value: unknown) => new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD' }).format(Number(value) || 0);
const human = (s: string) => s.replaceAll('_', ' ');
const tabs = ['Overview', 'Purchases', 'Documents', 'Actions', 'Prices', 'Benefits', 'Account'] as const;
type Tab = typeof tabs[number];

function Field({ label, children }: { label: string; children: ReactNode }) { return <label className="field"><span>{label}</span>{children}</label>; }
function Empty({ children }: { children: ReactNode }) { return <div className="empty-state">{children}</div>; }
function Modal({ title, close, children }: { title: string; close: () => void; children: ReactNode }) {
  const ref = useRef<HTMLDialogElement>(null);
  useEffect(() => { ref.current?.showModal(); }, []);
  return <dialog ref={ref} className="editor" onCancel={close}><div className="section-head"><h2>{title}</h2><button type="button" className="quiet" onClick={close} aria-label="Close editor">Close</button></div>{children}</dialog>;
}

export default function App() {
  const [config, setConfig] = useState<Config | null>(null);
  const [client, setClient] = useState<SupabaseClient | null>(null);
  const [session, setSession] = useState<Session | null>(null);
  const [authReady, setAuthReady] = useState(false);
  const [view, setView] = useState(location.pathname === '/desk' ? 'desk' : 'home');
  const [tab, setTab] = useState<Tab>('Overview');
  const [data, setData] = useState<Workspace | null>(null);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [busy, setBusy] = useState(false);
  const [loading, setLoading] = useState(false);
  const [email, setEmail] = useState('');
  const [otp, setOtp] = useState('');
  const [sent, setSent] = useState(false);
  const [linkedSender, setLinkedSender] = useState<string | null>(null);
  const [linkCode, setLinkCode] = useState('');
  const [drafts, setDrafts] = useState<Draft[]>([]);
  const [actionDraft, setActionDraft] = useState<{ product_id: string; case_type: string; due_date: string } | null>(null);
  const [claimDraft, setClaimDraft] = useState<any | null>(null);
  const [benefitOpen, setBenefitOpen] = useState(false);
  const [priceOpen, setPriceOpen] = useState(false);
  const [deleteText, setDeleteText] = useState('');
  const [emailText, setEmailText] = useState('');

  useEffect(() => {
    let subscription: { unsubscribe: () => void } | undefined;
    let alive = true;
    fetch('/api/config').then(r => { if (!r.ok) throw new Error('Could not connect to Lodestone.'); return r.json(); }).then(async (cfg: Config) => {
      if (!alive) return;
      setConfig(cfg);
      if (cfg.demo) { setAuthReady(true); return; }
      if (!cfg.supabase_url || !cfg.supabase_anon_key) throw new Error('Account configuration is incomplete. Contact the app administrator.');
      const auth = createClient(cfg.supabase_url, cfg.supabase_anon_key);
      setClient(auth);
      const { data: initial, error } = await auth.auth.getSession();
      if (error) throw error;
      if (!alive) return;
      setSession(initial.session);
      setAuthReady(true);
      subscription = auth.auth.onAuthStateChange((_event, value) => {
        setSession(value);
        if (!value) { setData(null); setDrafts([]); setClaimDraft(null); setActionDraft(null); setLinkedSender(null); setLinkCode(''); }
      }).data.subscription;
    }).catch(e => { if (alive) setError(e.message); });
    return () => { alive = false; subscription?.unsubscribe(); };
  }, []);

  async function api(path: string, options: RequestInit = {}) {
    const headers = new Headers(options.headers);
    if (!(options.body instanceof FormData) && options.body) headers.set('Content-Type', 'application/json');
    if (client) {
      const current = await client.auth.getSession();
      if (current.error) throw current.error;
      if (!current.data.session) throw new Error('Please sign in to continue.');
      headers.set('Authorization', `Bearer ${current.data.session.access_token}`);
    }
    const response = await fetch(path, { ...options, headers });
    if (!response.ok) {
      const payload = await response.json().catch(() => ({}));
      const detail = Array.isArray(payload.detail) ? payload.detail.map((x: any) => x.msg).join(' ') : payload.detail;
      throw new Error(detail || `Request failed (${response.status}). Please retry.`);
    }
    return response;
  }
  async function json(path: string, method = 'GET', body?: unknown) {
    return (await api(path, { method, body: body === undefined ? undefined : JSON.stringify(body) })).json();
  }
  async function refresh() {
    setData(await json('/api/workspace'));
    if (!config?.demo) setLinkedSender((await json('/api/account')).sender_id);
  }
  useEffect(() => {
    if (!authReady || view !== 'desk' || (!config?.demo && !session)) return;
    let active = true;
    const abort = new AbortController();
    const load = async () => {
      setLoading(true);
      try {
        const payload = await (await api('/api/workspace', { signal: abort.signal })).json();
        if (active) setData(payload);
        if (!config?.demo) {
          const account = await (await api('/api/account', { signal: abort.signal })).json();
          if (active) setLinkedSender(account.sender_id);
        }
      } catch (e) { if (active) setError((e as Error).message); }
      finally { if (active) setLoading(false); }
    };
    void load();
    const interval = window.setInterval(() => void load(), 30000);
    return () => { active = false; abort.abort(); clearInterval(interval); };
  }, [authReady, view, session?.user.id, config?.demo]);

  async function run(task: () => Promise<unknown>, success = '', reload = true) {
    setBusy(true); setError(''); setNotice('');
    try { await task(); if (reload) await refresh(); setNotice(success); }
    catch (e) { setError((e as Error).message); }
    finally { setBusy(false); }
  }
  async function download(path: string, filename: string) {
    const response = await api(path);
    const url = URL.createObjectURL(await response.blob());
    const link = document.createElement('a'); link.href = url; link.download = filename; link.click();
    window.setTimeout(() => URL.revokeObjectURL(url), 1000);
  }
  function productName(id: string) { return data?.products.find(p => p.id === id)?.product_name || 'Saved purchase'; }
  function newAction(product_id: string, case_type = 'return') { setActionDraft({ product_id, case_type, due_date: '' }); }
  function editProduct(p: Product) {
    setDrafts([{ ...emptyDraft(), ...p.identifiers, identifiers: p.identifiers,
      id: p.id, product_name: p.product_name, brand: p.brand || '',
      amount_paid: p.identifiers.amount_paid || '', merchant: p.identifiers.merchant || '',
      purchase_date: p.identifiers.purchase_date || '', payment_card: p.identifiers.payment_card || '',
      order_number: p.identifiers.order_number || '', model_number: p.identifiers.model_number || '',
      date_code: p.identifiers.date_code || '', upc: p.identifiers.upc || '' }]);
  }
  function review(result: any) {
    const items = result.items?.length ? result.items : [{}];
    setDrafts(items.map((item: any, index: number) => ({ ...emptyDraft(), ...Object.fromEntries(Object.entries(item).filter(([_, value]) => typeof value === 'string')),
      source_fingerprint: result.fingerprint, item_index: index + 1,
      amount_paid: Number.isFinite(Number(item.amount_paid)) && item.amount_paid != null ? String(Number(item.amount_paid)) : '',
      purchase_date: /^\d{4}-\d{2}-\d{2}$/.test(item.purchase_date || '') ? item.purchase_date : '' })));
    setNotice(result.note || 'Original saved. Review each item before saving it to your purchases.');
  }
  function openClaim(action: Action) {
    const existing = data?.claims.find(r => r.payload.case_id === action.id)?.payload;
    setClaimDraft(existing || { case_id: action.id, merchant: data?.products.find(p => p.id === action.product_id)?.identifiers.merchant || '', issue: '', portal_url: '', status: 'draft', recovered_amount: 0 });
  }
  const activeProducts = data?.products.filter(p => p.status === 'owned') || [];
  const actions = data?.recovery_cases || [];
  const openActions = actions.filter(a => a.status === 'open');
  const totalRecovered = data?.claims.reduce((sum, r) => sum + (r.payload.status === 'won' ? Number(r.payload.recovered_amount) : 0), 0) || 0;
  const flash = <>{error && <div className="flash error" role="alert">{error}<button className="quiet" onClick={() => setError('')}>Dismiss</button></div>}{notice && <div className="flash success" role="status">{notice}</div>}</>;

  if (!config || !authReady) return <main className="loading-screen"><div className="brand">Lodestone</div><p>{error || 'Opening your purchase workspace…'}</p>{error && <button onClick={() => location.reload()}>Retry</button>}</main>;
  if (view === 'home') return <div className="page"><section className="hero"><div className="hero-media" aria-hidden="true"/><div className="hero-top"><div className="brand">Lodestone</div><span className="live-dot">Every purchase, on record.</span></div><div className="hero-copy"><p className="eyebrow">More value from what you already own</p><h1>Save the receipt once. Put it to work later.</h1><p>A home for receipts, return dates, warranties, price watches, and the details you need when it’s time to recover money.</p><div className="cta-row"><button className="btn btn-primary" onClick={() => { setView('desk'); history.replaceState(null, '', '/desk'); }}>Open my workspace</button>{config.imessage_number && <a className="btn btn-ghost" href={`sms:${config.imessage_number}?body=HELP`}>Text Lodestone</a>}</div><p className="hero-footnote">Keep originals. Review the details. Know your next step.</p>{config.demo && <p className="demo-pill">Local demo · sample data is labeled · records reset on restart</p>}</div></section></div>;
  if (!config.demo && !session) return <main className="auth-card"><p className="eyebrow">Every purchase, on record.</p><h1>{sent ? 'Check your email' : 'Welcome to Lodestone'}</h1><p>{sent ? `Enter the code sent to ${email}.` : 'Sign in or create your account with an email code.'}</p>{flash}<form onSubmit={e => { e.preventDefault(); void run(async () => {
    const result = sent ? await client!.auth.verifyOtp({ email, token: otp, type: 'email' }) : await client!.auth.signInWithOtp({ email });
    if (result.error) throw result.error;
    if (!sent) setSent(true);
  }, '', false); }}><Field label={sent ? 'Email code' : 'Email address'}>{sent ? <input value={otp} onChange={e => setOtp(e.target.value)} autoComplete="one-time-code" inputMode="numeric" required/> : <input value={email} onChange={e => setEmail(e.target.value)} type="email" autoComplete="email" required/>}</Field><button className="primary" disabled={busy}>{busy ? 'Please wait…' : sent ? 'Sign in' : 'Send a code'}</button></form>{sent && <button className="quiet" disabled={busy} onClick={() => { setSent(false); setOtp(''); }}>Use another email or resend</button>}<button className="quiet" onClick={() => setView('home')}>Back to home</button></main>;

  const productSelect = (value: string, change: (id: string) => void) => <Field label="Purchase"><select value={value} onChange={e => change(e.target.value)} required><option value="">Choose a purchase</option>{activeProducts.map(p => <option key={p.id} value={p.id}>{p.product_name}</option>)}</select></Field>;
  const actionRows = (rows: Action[]) => rows.length ? <div className="card-list">{rows.map(a => <article className="record" key={a.id}><div><span className="badge">{human(a.case_type)}</span><h3>{productName(a.product_id)}</h3><p>{a.due_date} · {human(a.status)}</p></div><div className="record-actions"><button disabled={busy} onClick={() => openClaim(a)}>Prepare claim</button><button disabled={busy} onClick={() => void run(() => download(`/api/workspace/claims/${a.id}/packet`, 'lodestone-claim.txt'), 'Claim packet downloaded.', false)}>Download packet</button>{a.status === 'open' ? <><button disabled={busy} onClick={() => void run(() => json(`/api/workspace/actions/${a.id}`, 'PATCH', { status: 'completed' }), 'Action completed.')}>Complete</button><button className="quiet" disabled={busy} onClick={() => void run(() => json(`/api/workspace/actions/${a.id}`, 'PATCH', { status: 'not_applicable' }), 'Action dismissed.')}>Dismiss</button></> : <button disabled={busy} onClick={() => void run(() => json(`/api/workspace/actions/${a.id}`, 'PATCH', { status: 'open' }), 'Action reopened.')}>Reopen</button>}</div></article>)}</div> : <Empty>No deadlines yet. Add a date from your receipt or the merchant’s terms.</Empty>;

  return <div className="workspace-shell">
    <header className="app-header"><button className="wordmark" onClick={() => setView('home')}>Lodestone<span>Every purchase, on record.</span></button><div className="header-actions">{config.demo ? <span className="badge">Demo · resets on restart</span> : <span className="user-email">{session?.user.email}</span>}<button disabled={busy || loading} onClick={() => void run(refresh, '', false)}>Refresh</button>{client && <button disabled={busy} onClick={() => void run(async () => { const { error } = await client.auth.signOut(); if (error) throw error; }, '', false)}>Sign out</button>}</div></header>
    <div className="workspace-layout"><nav className="side-nav" aria-label="Workspace navigation">{tabs.map(t => <button key={t} className={tab === t ? 'selected' : ''} aria-current={tab === t ? 'page' : undefined} onClick={() => { setTab(t); setError(''); setNotice(''); }}>{t}</button>)}</nav>
    <main className="workspace-main"><div className="section-head"><div><p className="eyebrow">Your purchase workspace</p><h1>{tab}</h1></div>{data && <small className="updated">Updated {new Date(data.generated_at).toLocaleTimeString()}</small>}</div>{flash}{loading && !data && <p role="status">Loading your records…</p>}
    {tab === 'Overview' && <><section className="stat-grid" aria-label="Purchase summary"><div><span>Saved purchases</span><strong>{activeProducts.length}</strong></div><div><span>Open actions</span><strong>{openActions.length}</strong></div><div><span>Originals retained</span><strong>{data?.documents.length || 0}</strong></div><div><span>Money recovered</span><strong>{dollars(totalRecovered)}</strong></div></section><section className="welcome-panel"><div><p className="eyebrow">Make your next purchase count</p><h2>Start with the receipt.</h2><p>Add a photo, invoice PDF, or receipt email. Review the details, then track the dates and opportunities that matter.</p></div><div className="record-actions"><button className="primary" onClick={() => setTab('Documents')}>Upload a receipt</button><button onClick={() => { setDrafts([emptyDraft()]); }}>Add purchase manually</button>{config.demo && <button disabled={busy} onClick={() => void run(() => json('/api/workspace/demo-seed', 'POST'), 'Historical demo purchase loaded. Open Purchases to view it.')}>Load sample purchase</button>}</div></section><h2>Open deadlines</h2>{actionRows(openActions.slice(0, 5))}<p className="fine-print">Dates are based on the records and terms you enter. Link iMessage in Account to receive reminders while the messaging service is running.</p></>}
    {tab === 'Purchases' && <><div className="toolbar"><button className="primary" onClick={() => setDrafts([emptyDraft()])}>Add purchase</button><button onClick={() => setTab('Documents')}>Import receipt</button></div>{!data?.products.length ? <Empty>Your purchases will appear here after you confirm their details.</Empty> : <div className="purchase-grid">{data.products.map(p => {
      const recall = data.recall_checks.find(r => r.payload.product_id === p.id)?.payload.result;
      return <article className="purchase-card" key={p.id}><div className="section-head"><span className="badge">{human(p.status)}</span><span className="price">{p.identifiers.amount_paid ? dollars(p.identifiers.amount_paid) : 'Amount unknown'}</span></div><h2>{p.product_name}</h2><p>{[p.brand, p.identifiers.merchant].filter(Boolean).join(' · ') || 'Merchant not recorded'}</p><dl><div><dt>Purchased</dt><dd>{p.identifiers.purchase_date || 'Not recorded'}</dd></div><div><dt>Model / code</dt><dd>{[p.identifiers.model_number, p.identifiers.date_code].filter(Boolean).join(' / ') || 'Not recorded'}</dd></div><div><dt>Payment card label</dt><dd>{p.identifiers.payment_card || 'Not recorded'}</dd></div></dl><div className="record-actions"><button disabled={busy} onClick={() => editProduct(p)}>Edit details</button>{p.status === 'owned' && <button disabled={busy} onClick={() => newAction(p.id)}>Track a date</button>}<button disabled={busy} onClick={() => void run(() => json(`/api/workspace/products/${p.id}/recall`, 'POST'), 'Recall sources checked.')}>Check recalls</button>{p.receipt_fingerprint && <button disabled={busy} onClick={() => void run(() => download(`/api/workspace/documents/${p.receipt_fingerprint}`, 'receipt'), 'Original downloaded.', false)}>Get receipt</button>}{p.status === 'owned' && <button className="quiet" disabled={busy} onClick={() => void run(() => json(`/api/workspace/products/${p.id}/archive`, 'POST'), 'Purchase archived; its open dates and price watches were stopped.')}>No longer owned</button>}</div>{recall && <div className="recall-result"><strong>{human(recall.outcome || 'Source check')}</strong><p>{recall.summary}</p>{recall.notice?.url && <a href={recall.notice.url} target="_blank" rel="noreferrer">Official recall notice ↗</a>}{recall.candidates?.map((c: any, i: number) => <p key={i}><a href={c.url || c.official_url} target="_blank" rel="noreferrer">{c.title || 'Official notice'} ↗</a></p>)}{recall.used_historical_fixture && <span className="badge">Historical demonstration notice</span>}</div>}</article>;
    })}</div>}<p className="fine-print">Recall searches check CPSC sources. No match does not establish that a product is safe. Compare exact identifiers with each official notice.</p></>}
    {tab === 'Documents' && <><div className="two-column"><section className="panel"><h2>Keep the original</h2><p>Upload a receipt photo, PDF invoice, or exported receipt email (.eml). Originals stay in your private records even when extraction cannot read them.</p><form onSubmit={e => { e.preventDefault(); const form = new FormData(e.currentTarget); void run(async () => review(await (await api('/api/workspace/documents', { method: 'POST', body: form })).json())); }}><Field label="Receipt or invoice (up to 10 MB)"><input name="file" type="file" accept=".jpg,.jpeg,.png,.webp,.pdf,.eml" required/></Field><button className="primary" disabled={busy}>{busy ? 'Saving and reading…' : 'Upload and review'}</button></form></section><section className="panel"><h2>Import receipt email text</h2><p>Copy the purchase details from an email. You can also download “original message” in your mail app and upload its .eml file.</p><form onSubmit={e => { e.preventDefault(); void run(async () => { review(await json('/api/workspace/email-text', 'POST', { text: emailText })); setEmailText(''); }); }}><Field label="Receipt email text"><textarea required minLength={10} maxLength={100000} rows={5} value={emailText} onChange={e => setEmailText(e.target.value)}/></Field><button disabled={busy}>Import email text</button></form></section></div><h2>Saved originals</h2>{!data?.documents.length ? <Empty>No documents saved yet.</Empty> : <div className="card-list">{data.documents.map(d => <article className="record" key={d.fingerprint}><div><h3>{d.filename}</h3><p>{d.mime_type} · {d.created_at ? new Date(d.created_at).toLocaleDateString() : 'Retained original'}</p></div><div className="record-actions"><button disabled={busy} onClick={() => void run(() => download(`/api/workspace/documents/${d.fingerprint}`, d.filename), 'Original downloaded.', false)}>Download</button><button onClick={() => setDrafts([{ ...emptyDraft(), source_fingerprint: d.fingerprint }])}>Add purchase from this</button></div></article>)}</div>}</>}
    {tab === 'Actions' && <><div className="toolbar"><button className="primary" disabled={!activeProducts.length} onClick={() => newAction(activeProducts[0].id)}>Track a deadline</button></div><p>Track returns, warranties, price adjustments, rebates, and card benefits. Prepare your packet, review the merchant’s terms, submit through their channel, and record the result here.</p>{actionRows(actions)}<h2>Claim progress</h2>{!data?.claims.length ? <Empty>Use “Prepare claim” on an action to record an issue and submission channel.</Empty> : data.claims.map(r => <article className="record" key={r.id}><div><span className="badge">{human(r.payload.status)}</span><h3>{r.payload.merchant} · {productName(r.payload.product_id)}</h3><p>{r.payload.issue}</p>{r.payload.status === 'won' && <strong>Recovered {dollars(r.payload.recovered_amount)}</strong>}</div><div className="record-actions"><button onClick={() => setClaimDraft(r.payload)}>Update claim</button>{r.payload.portal_url && <a className="button" href={r.payload.portal_url} target="_blank" rel="noreferrer">Open merchant channel ↗</a>}</div></article>)}</>}
    {tab === 'Prices' && <><div className="toolbar"><button className="primary" disabled={!activeProducts.length} onClick={() => setPriceOpen(true)}>Add Amazon price watch</button></div><p>Set a target for an Amazon.com listing. Price checks use Keepa’s latest recorded Amazon price and may lag the live listing. A lower price does not guarantee a refund.</p>{!config.prices_enabled && <div className="flash">Live price checking has not been enabled by the app administrator. You can save targets now.</div>}{!data?.price_watches.length ? <Empty>No price watches. Add a purchase first, then its Amazon listing.</Empty> : data.price_watches.map(w => <article className="record" key={w.id}><div><span className="badge">{w.is_active ? 'Active' : 'Paused'}</span><h3>{productName(w.product_id)}</h3><p>Target {dollars(w.target_price)} · {w.current_price === null ? 'Not checked yet' : `Last recorded ${dollars(w.current_price)}`}</p>{w.last_checked_at && <small>Checked {new Date(w.last_checked_at).toLocaleString()}</small>}{w.check_error ? <p className="error">{w.check_error} Any previous price is not current.</p> : w.current_price !== null && w.current_price <= w.target_price ? <p className="positive">Target reached · review the listing and refund terms</p> : null}</div><div className="record-actions"><a className="button" href={w.product_url} target="_blank" rel="noreferrer">Open listing ↗</a>{w.is_active && <><button disabled={busy || !config.prices_enabled} onClick={() => void run(() => json(`/api/workspace/prices/${w.id}/check`, 'POST'), 'Price record updated.')}>Check now</button><button disabled={busy} onClick={() => void run(() => json(`/api/workspace/prices/${w.id}/pause`, 'POST'), 'Watch paused.')}>Pause</button></>}</div></article>)}</>}
    {tab === 'Benefits' && <><div className="toolbar"><button className="primary" onClick={() => setBenefitOpen(true)}>Add rebate or card benefit</button></div><p>Record an offer from its official terms. Lodestone compares the merchant, purchase dates, and card label with your saved purchases. Matches are leads to review, not confirmed eligibility.</p>{data?.benefit_matches.map((m, i) => <article className="record match-card" key={i}><div><span className="badge">{human(m.status)}</span><h3>{m.name} · {productName(m.product_id)}</h3><p>Recorded cap: {dollars(m.amount_cap)}{m.missing.length ? ` · Add ${m.missing.join(', ')}` : ''}</p><small>{m.reason}</small></div><button onClick={() => newAction(m.product_id, data.benefits.find(b => b.id === m.benefit_id)?.payload.kind || 'rebate')}>Track recovery</button></article>)}{!data?.benefits.length ? <Empty>Add terms for a rebate or card benefit you want to track.</Empty> : data.benefits.map(b => <article className="record" key={b.id}><div><span className="badge">{human(b.payload.kind)}</span><h3>{b.payload.name}</h3><p>{b.payload.merchant} · purchases {b.payload.starts_on} through {b.payload.ends_on} · cap {dollars(b.payload.amount_cap)}</p><p>{b.payload.terms}</p>{b.payload.payment_card && <small>Card label: {b.payload.payment_card}</small>}</div><div className="record-actions">{b.payload.terms_url && <a className="button" href={b.payload.terms_url} target="_blank" rel="noreferrer">Review terms ↗</a>}<button disabled={busy} onClick={() => void run(() => json(`/api/workspace/benefits/${b.id}`, 'DELETE'), 'Benefit removed.')}>Remove</button></div></article>)}</>}
    {tab === 'Account' && <div className="two-column"><section className="panel"><h2>Optional iMessage connection</h2>{config.demo ? <p>Pairing is available when the app is configured with live accounts. All browser features can be demonstrated here without a phone.</p> : <>{linkedSender ? <p>Linked to {linkedSender}. Send receipts from this account, and receive tracked deadline and price alerts.</p> : <><p>The browser works on its own. To add receipts by iMessage and receive alerts, create a code and text it to Lodestone.</p><button disabled={busy} onClick={() => void run(async () => setLinkCode((await json('/api/account/link-code', 'POST')).code), '', false)}>Create pairing code</button>{linkCode && <p className="link-code">LINK {linkCode}</p>}<p className="fine-print">Expires in 10 minutes. Keep this code private.</p></>}{config.imessage_number && <a href={`sms:${config.imessage_number}?body=${encodeURIComponent(linkCode ? `LINK ${linkCode}` : 'HELP')}`}>Open Messages ↗</a>}</>}</section><section className="panel"><h2>Your records</h2><p>Original receipts are retained. Product details are saved after your review. Export your workspace data, and download originals from Documents.</p><button disabled={busy} onClick={() => void run(() => download('/api/workspace/export', 'lodestone-records.json'), 'Workspace exported.', false)}>Export records</button><hr/><h3>Delete saved purchase data</h3><p>This permanently removes purchase records, receipts, actions, benefits, and claims. Your sign-in and phone link remain available.</p><Field label="Type DELETE to confirm"><input value={deleteText} onChange={e => setDeleteText(e.target.value)} autoComplete="off"/></Field><button className="danger" disabled={busy || deleteText !== 'DELETE'} onClick={() => void run(async () => { await json('/api/workspace', 'DELETE'); setDeleteText(''); }, 'Your saved purchase data was deleted.')}>Delete my purchase data</button></section></div>}
    </main></div>
    {!!drafts.length && <Modal title={drafts[0].id ? 'Edit purchase' : `Review purchase${drafts.length > 1 ? ` · ${drafts.length} remaining` : ''}`} close={() => setDrafts([])}>{error && <p className="error" role="alert">{error}</p>}<p>Your original is retained. Confirm only details you can verify.</p><form onSubmit={e => { e.preventDefault(); const d = drafts[0]; void run(async () => {
      const identifiers = { ...d.identifiers, model_number: d.model_number, date_code: d.date_code, upc: d.upc };
      const body = { product_name: d.product_name, brand: d.brand, merchant: d.merchant, purchase_date: d.purchase_date || null, amount_paid: d.amount_paid === '' ? null : Number(d.amount_paid), payment_card: d.payment_card, order_number: d.order_number, identifiers: Object.fromEntries(Object.entries(identifiers).filter(([_, v]) => typeof v === 'string')), source_fingerprint: d.source_fingerprint || null, item_index: d.item_index };
      await json(`/api/workspace/products${d.id ? `/${d.id}` : ''}`, d.id ? 'PUT' : 'POST', body);
      setDrafts(current => current.slice(1));
    }, 'Purchase saved.'); }}><div className="form-grid">{([['product_name','Product name','text'],['brand','Brand','text'],['merchant','Merchant','text'],['purchase_date','Purchase date','date'],['amount_paid','Amount paid (USD)','number'],['payment_card','Card label or last four (optional)','text'],['order_number','Order / receipt number','text'],['model_number','Model number','text'],['date_code','Date code','text'],['upc','UPC','text']] as const).map(([key,label,type]) => <Field label={label} key={key}><input type={type} required={key === 'product_name'} step={type === 'number' ? '0.01' : undefined} min={type === 'number' ? '0' : undefined} maxLength={160} value={drafts[0][key] || ''} onChange={e => setDrafts(current => [{ ...current[0], [key]: e.target.value }, ...current.slice(1)])}/></Field>)}</div><div className="toolbar"><button className="primary" disabled={busy}>Confirm and save</button><button type="button" disabled={busy} onClick={() => setDrafts(current => current.slice(1))}>Skip item</button></div></form></Modal>}
    {actionDraft && <Modal title="Track a deadline" close={() => setActionDraft(null)}>{error && <p className="error" role="alert">{error}</p>}<form onSubmit={e => { e.preventDefault(); void run(async () => { await json('/api/workspace/actions', 'POST', actionDraft); setActionDraft(null); }, 'Deadline saved.'); }}>{productSelect(actionDraft.product_id, id => setActionDraft({ ...actionDraft, product_id: id }))}<Field label="Action"><select value={actionDraft.case_type} onChange={e => setActionDraft({ ...actionDraft, case_type: e.target.value })}>{['return','warranty','price_adjustment','rebate','card_benefit'].map(k => <option key={k} value={k}>{human(k)}</option>)}</select></Field><Field label="Deadline confirmed from your terms"><input type="date" required value={actionDraft.due_date} onChange={e => setActionDraft({ ...actionDraft, due_date: e.target.value })}/></Field><p className="fine-print">You confirm this date. If iMessage is linked, Lodestone sends an upcoming reminder while its messaging service is online.</p><button className="primary" disabled={busy}>Save deadline</button></form></Modal>}
    {claimDraft && <Modal title="Prepare and track your claim" close={() => setClaimDraft(null)}>{error && <p className="error" role="alert">{error}</p>}<form onSubmit={e => { e.preventDefault(); void run(async () => { const { product_id, ...body } = claimDraft; await json('/api/workspace/claims', 'PUT', { ...body, recovered_amount: Number(body.recovered_amount) }); setClaimDraft(null); }, 'Claim saved. Download the packet from its action when ready.'); }}><Field label="Merchant or claim recipient"><input required maxLength={100} value={claimDraft.merchant} onChange={e => setClaimDraft({ ...claimDraft, merchant: e.target.value })}/></Field><Field label="What happened and what you are requesting"><textarea rows={5} required maxLength={4000} value={claimDraft.issue} onChange={e => setClaimDraft({ ...claimDraft, issue: e.target.value })}/></Field><Field label="Merchant submission or support URL (optional HTTPS link)"><input type="url" placeholder="https://…" value={claimDraft.portal_url} onChange={e => setClaimDraft({ ...claimDraft, portal_url: e.target.value })}/></Field><Field label="Progress"><select value={claimDraft.status} onChange={e => setClaimDraft({ ...claimDraft, status: e.target.value, recovered_amount: e.target.value === 'won' ? claimDraft.recovered_amount : 0 })}>{['draft','submitted','won','denied'].map(s => <option key={s}>{s}</option>)}</select></Field>{claimDraft.status === 'won' && <Field label="Money actually recovered (USD)"><input type="number" min={0} step="0.01" required value={claimDraft.recovered_amount} onChange={e => setClaimDraft({ ...claimDraft, recovered_amount: e.target.value })}/></Field>}<p className="fine-print">Save, download your packet and receipt, then submit through the merchant’s channel. Mark it submitted only after you send it. Lodestone does not send this claim for you.</p><button className="primary" disabled={busy}>Save claim</button></form></Modal>}
    {priceOpen && <Modal title="Add Amazon price watch" close={() => setPriceOpen(false)}>{error && <p className="error" role="alert">{error}</p>}<form onSubmit={e => { e.preventDefault(); const f = new FormData(e.currentTarget); void run(async () => { await json('/api/workspace/prices','POST',{ product_id: f.get('product_id'), url: f.get('url'), target_price: Number(f.get('target_price')) }); setPriceOpen(false); }, 'Price watch saved.'); }}><Field label="Purchase"><select name="product_id" required>{activeProducts.map(p => <option value={p.id} key={p.id}>{p.product_name}</option>)}</select></Field><Field label="Amazon.com product URL"><input type="url" name="url" required placeholder="https://www.amazon.com/dp/…"/></Field><Field label="Alert at or below (USD)"><input type="number" name="target_price" min="0.01" max="100000" step="0.01" required/></Field><button className="primary" disabled={busy}>Save watch</button></form></Modal>}
    {benefitOpen && <Modal title="Record benefit terms" close={() => setBenefitOpen(false)}>{error && <p className="error" role="alert">{error}</p>}<form onSubmit={e => { e.preventDefault(); const f = new FormData(e.currentTarget); void run(async () => { await json('/api/workspace/benefits','POST',{ ...Object.fromEntries(f.entries()), amount_cap: Number(f.get('amount_cap')) }); setBenefitOpen(false); }, 'Benefit saved. Potential matches are shown on Benefits.'); }}><Field label="Offer name"><input name="name" maxLength={120} required/></Field><Field label="Type"><select name="kind"><option value="rebate">Rebate</option><option value="card_benefit">Card benefit</option></select></Field><div className="form-grid"><Field label="Merchant exactly as recorded on purchases"><input name="merchant" required maxLength={100}/></Field><Field label="Required card label (required for card benefits)"><input name="payment_card" maxLength={80}/></Field><Field label="Eligible purchases from"><input name="starts_on" type="date" required/></Field><Field label="Eligible purchases through"><input name="ends_on" type="date" required/></Field></div><Field label="Maximum benefit amount (USD)"><input name="amount_cap" type="number" min="0.01" max="100000" step="0.01" required/></Field><Field label="Official terms URL"><input type="url" name="terms_url" placeholder="https://…"/></Field><Field label="Terms, exclusions, and submission deadline"><textarea name="terms" required maxLength={4000} rows={4}/></Field><button className="primary" disabled={busy}>Save terms and find matches</button></form></Modal>}
  </div>;
}
