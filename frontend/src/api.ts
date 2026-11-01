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

export interface Ticket {
  id: string;
  type: TicketType;
  title: string;
  tracking_number: string | null;
  description: string;
  status: string;
  creator_name: string;
  assignee_name: string | null;
  created_at: string;
}

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

export class ApiError extends Error {
  constructor(
    readonly status: number,
    message: string,
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
  // FastAPI 的 422 校验错误是一个数组
  if (status === 422) return "填写的内容不符合要求，请检查后重试";
  return "请求失败，请稍后再试";
}

export async function api<T>(path: string, options: { method?: string; body?: unknown } = {}): Promise<T> {
  let response: Response;
  try {
    response = await fetch(path, {
      method: options.method ?? "GET",
      headers: options.body === undefined ? undefined : { "Content-Type": "application/json" },
      body: options.body === undefined ? undefined : JSON.stringify(options.body),
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
    throw new ApiError(response.status, errorMessage(data?.detail, response.status));
  }
  return data as T;
}
