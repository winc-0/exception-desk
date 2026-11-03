import { beforeEach, describe, expect, it, vi } from "vitest";
import type { Me } from "./api";
import { router } from "./router";
import { me } from "./session";

vi.mock("./session", async () => {
  const { ref } = await import("vue");
  const value = ref<Me | null>(null);
  return {
    me: value,
    restoreSession: async () => {},
    can: (permission: string) => value.value?.permissions.includes(permission) ?? false,
  };
});

function signIn(...permissions: string[]) {
  me.value = { permissions } as Me;
}

async function visit(path: string): Promise<string> {
  await router.push(path);
  return router.currentRoute.value.path;
}

describe("路由权限", () => {
  beforeEach(() => {
    me.value = null;
  });

  it("没登录时跳到登录页", async () => {
    await expect(visit("/tickets")).resolves.toBe("/login");
    await expect(visit("/claims")).resolves.toBe("/login");
  });

  it("已登录时不再显示登录页", async () => {
    signIn();
    await visit("/tickets");
    await expect(visit("/login")).resolves.toBe("/tickets");
  });

  it("没有权限的页面回到工单列表", async () => {
    signIn();
    await expect(visit("/claims")).resolves.toBe("/tickets");
    await expect(visit("/members")).resolves.toBe("/tickets");
  });

  it("有权限的页面可以进入", async () => {
    signIn("claims:manage", "members:read");
    await expect(visit("/claims")).resolves.toBe("/claims");
    await expect(visit("/members")).resolves.toBe("/members");
  });

  it("未知地址回到工单列表", async () => {
    signIn();
    await expect(visit("/nope")).resolves.toBe("/tickets");
  });
});
