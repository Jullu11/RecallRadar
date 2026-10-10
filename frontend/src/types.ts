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

export type DashboardPayload = {
  products: Product[];
  matches: MatchRow[];
  notices: NoticeRow[];
  follow_ups: FollowUp[];
  persistence: string;
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
