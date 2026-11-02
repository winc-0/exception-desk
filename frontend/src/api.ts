export type Role = "admin" | "supervisor" | "agent" | "warehouse";
export type TicketType = "damaged" | "lost" | "delayed" | "address_change" | "other";

export interface Me {
  user_id: string;
  email: string;
  display_name: string;
  role: Role;
  permissions: string[];
  tenant: { slug: string; name: string };
}

export interface Member {
  user_id: string;
  email: string;
  display_name: string;
  role: Role;
  is_active: boolean;
}

export type TicketStatus = "open" | "in_progress" | "waiting" | "resolved" | "closed" | "cancelled";

export interface Ticket {
  id: string;
  number: number;
  version: number;
  type: TicketType;
  title: string;
  tracking_number: string | null;
  description: string;
  status: TicketStatus;
  resolution: string;
  creator_name: string;
  assignee_id: string | null;
  assignee_name: string | null;
  created_at: string;
  updated_at: string;
}

export interface TicketPage {
  items: Ticket[];
  next_before: number | null;
}

export interface TicketEvent {
  id: number;
  kind:
    | "created"
    | "status_changed"
    | "assigned"
    | "commented"
    | "attachment_added"
    | "attachment_voided"
    | "claim_opened"
    | "claim_updated"
    | "claim_status_changed";
  actor_name: string;
  from_status: TicketStatus | null;
  to_status: TicketStatus | null;
  body: string;
  data: {
    assignee_name?: string | null;
    source?: string;
    category_label?: string;
    filename?: string;
    carrier?: string;
    from?: ClaimStatus;
    to?: ClaimStatus;
    approved_cents?: number;
    paid_cents?: number;
  };
  created_at: string;
}

export type EvidenceCategory =
  | "label_photo"
  | "package_photo"
  | "damage_photo"
  | "packing_video"
  | "weight_record"
  | "tracking_screenshot"
  | "value_proof"
  | "customer_proof"
  | "other";

export interface Attachment {
  id: string;
  category: EvidenceCategory;
  filename: string;
  content_type: string;
  size_bytes: number;
  sha256: string;
  uploader_name: string;
  created_at: string;
  voided_at: string | null;
  void_reason: string;
  can_void: boolean;
}

export interface ChecklistItem {
  category: EvidenceCategory;
  label: string;
  level: "required" | "recommended" | "optional";
  count: number;
}

export type ClaimStatus = "preparing" | "submitted" | "approved" | "rejected" | "paid" | "withdrawn";

export interface Claim {
  version: number;
  status: ClaimStatus;
  carrier: string;
  claim_amount_cents: number | null;
  approved_cents: number | null;
  paid_cents: number | null;
  insured: boolean;
  pickup_weight_g: number | null;
  billed_weight_g: number | null;
  external_ref: string;
  rejection_reason: string;
  created_at: string;
  updated_at: string;
  submitted_at: string | null;
  paid_at: string | null;
}

export interface ClaimListItem extends Claim {
  ticket_id: string;
  ticket_number: number;
  ticket_title: string;
  ticket_type: TicketType;
  tracking_number: string | null;
}

export interface ClaimPage {
  items: ClaimListItem[];
  next_before: number | null;
}

export interface CarrierSummary {
  carrier: string;
  total: number;
  open: number;
  rejected: number;
  paid: number;
  claimed_cents: number;
  settled_claimed_cents: number;
  paid_cents: number;
}

export interface ClaimSummary {
  total: CarrierSummary;
  carriers: CarrierSummary[];
}

export interface TicketDetail extends Ticket {
  events: TicketEvent[];
  attachments: Attachment[];
  evidence_checklist: ChecklistItem[];
  can_upload: boolean;
  claim: Claim | null;
  claim_enabled: boolean;
  claim_transitions: ClaimStatus[];
  claim_blocks_closing: boolean;
  allowed_transitions: TicketStatus[];
  can_claim: boolean;
  can_assign: boolean;
  can_comment: boolean;
}

export interface ImportResult {
  created: number;
  skipped: { line: number; tracking_number: string | null; message: string }[];
}

export const STATUS_LABELS: Record<TicketStatus, string> = {
  open: "待处理",
  in_progress: "处理中",
  waiting: "等待外部",
  resolved: "已解决",
  closed: "已关闭",
  cancelled: "已取消",
};

export const CLAIM_STATUS_LABELS: Record<ClaimStatus, string> = {
  preparing: "准备中",
  submitted: "已提交",
  approved: "已核定",
  rejected: "被拒赔",
  paid: "已到账",
  withdrawn: "已撤回",
};

export const ROLE_LABELS: Record<Role, string> = {
  admin: "管理员",
  supervisor: "主管",
  agent: "客服",
  warehouse: "仓管",
};

export const TYPE_LABELS: Record<TicketType, string> = {
  damaged: "破损",
  lost: "丢件",
  delayed: "超时",
  address_change: "改址",
  other: "其他",
};

export interface RowError {
  line: number;
  message: string;
}

export class ApiError extends Error {
  constructor(
    readonly status: number,
    message: string,
    readonly rowErrors: RowError[] = [],
  ) {
    super(message);
  }
}

type Unauthorized = () => void;
let onUnauthorized: Unauthorized = () => {};

export function setUnauthorizedHandler(handler: Unauthorized) {
  onUnauthorized = handler;
}

function errorMessage(detail: unknown, status: number): string {
  if (typeof detail === "string") return detail;
  const message = (detail as { message?: unknown } | null)?.message;
  if (typeof message === "string") return message;
  // FastAPI 的 422 校验错误是一个数组
  if (status === 422) return "填写的内容不符合要求，请检查后重试";
  return "请求失败，请稍后再试";
}

export async function api<T>(
  path: string,
  options: { method?: string; body?: unknown; file?: File } = {},
): Promise<T> {
  let headers: Record<string, string> | undefined;
  let body: BodyInit | undefined;
  if (options.file) {
    headers = { "Content-Type": options.file.type || "application/octet-stream" };
    body = options.file;
  } else if (options.body !== undefined) {
    headers = { "Content-Type": "application/json" };
    body = JSON.stringify(options.body);
  }

  let response: Response;
  try {
    response = await fetch(path, {
      method: options.method ?? "GET",
      headers,
      body,
      credentials: "same-origin",
    });
  } catch {
    throw new ApiError(0, "无法连接服务器，请检查网络");
  }

  if (response.status === 204) return undefined as T;
  const data = await response.json().catch(() => null);
  if (!response.ok) {
    const isLogin = path === "/api/auth/login";
    if (response.status === 401 && !isLogin) onUnauthorized();
    throw new ApiError(
      response.status,
      errorMessage(data?.detail, response.status),
      data?.detail?.errors ?? [],
    );
  }
  return data as T;
}
