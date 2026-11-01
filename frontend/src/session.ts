import { ref } from "vue";
import { api, ApiError, type Me } from "./api";

export const me = ref<Me | null>(null);
let restored = false;

export async function restoreSession(): Promise<void> {
  if (restored) return;
  restored = true;
  try {
    me.value = await api<Me>("/api/auth/me");
  } catch (error) {
    if (!(error instanceof ApiError) || error.status !== 401) throw error;
  }
}

export function can(permission: string): boolean {
  return me.value?.permissions.includes(permission) ?? false;
}
