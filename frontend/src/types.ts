export type Decision = "affected" | "possible_match" | "no_match";

export type Product = {
  id: string;
  owner_handle?: string;
  brand: string | null;
  product_name: string;
  identifiers: Record<string, string | null>;
  status: string;
  monitoring_enabled: boolean;
  source_type?: string;
  field_provenance?: Record<string, {
    source?: string;
    source_type?: string;
    user_confirmed?: boolean;
    user_corrected?: boolean;
    confirmed_at?: string;
  }>;
  created_at?: string;
};

export type MatchRow = {
  id: string;
  product_id: string;
  notice_id: string;
  decision: Decision;
  evidence: Record<string, unknown>;
  checked_at: string;
};

export type NoticeRow = {
  id: string;
  agency: string;
  external_id: string;
  title: string;
  official_url: string;
  affected_criteria?: Record<string, unknown>;
  retrieved_at?: string;
  historical_fixture?: boolean;
};

export type FollowUp = {
  id: string;
  product_id: string;
  action: string;
  completed_at: string | null;
  created_at: string;
};

export type RecoveryCase = {
  id: string;
  product_id: string;
  case_type: "return" | "warranty" | "price_adjustment";
  due_date: string;
  status: "open" | "completed" | "not_applicable";
  date_source: "user_confirmed";
  created_at: string;
  completed_at: string | null;
};

export type DashboardPayload = {
  products: Product[];
  matches: MatchRow[];
  notices: NoticeRow[];
  follow_ups: FollowUp[];
  recovery_cases: RecoveryCase[];
  persistence: string;
  owner_handle?: string | null;
  demo_notice: {
    title: string;
    url: string;
    recall_number: string;
    recall_date: string;
    remedy: string;
    hazard: string;
    historical_fixture: boolean;
  };
  generated_at: string;
};

export type PurchaseInsights = {
  source: string;
  snowflake_enabled: boolean;
  used_cortex: boolean;
  summary: string;
  metrics: {
    product_count: number;
    monitoring_count: number;
    affected_count: number;
    possible_match_count: number;
    open_recovery_count: number;
    due_within_30_days: number;
    overdue_count: number;
  };
  generated_at: string;
};
