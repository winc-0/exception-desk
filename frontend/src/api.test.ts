// @vitest-environment node
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api, ApiError, setUnauthorizedHandler, streamEvents } from "./api";

const fetchMock = vi.fn<typeof fetch>();
const onUnauthorized = vi.fn();

function json(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), { status });
}

function stream(chunks: Uint8Array[]): Response {
  const body = new ReadableStream<Uint8Array>({
    start(controller) {
      chunks.forEach((chunk) => controller.enqueue(chunk));
      controller.close();
    },
  });
  return new Response(body, { status: 200 });
}

beforeEach(() => {
  fetchMock.mockReset();
  onUnauthorized.mockReset();
  vi.stubGlobal("fetch", fetchMock);
  setUnauthorizedHandler(onUnauthorized);
});

afterEach(() => setUnauthorizedHandler(() => {}));

describe("api", () => {
  it("发送 JSON 并带上 Cookie", async () => {
    fetchMock.mockResolvedValue(json(200, { ok: true }));
    await expect(api("/api/x", { method: "POST", body: { a: 1 } })).resolves.toEqual({ ok: true });
    const [, init] = fetchMock.mock.calls[0];
    expect(init?.body).toBe('{"a":1}');
    expect(init?.credentials).toBe("same-origin");
    expect(init?.headers).toEqual({ "Content-Type": "application/json" });
  });

  it("上传文件时不转成 JSON", async () => {
    fetchMock.mockResolvedValue(json(200, {}));
    const file = new File(["x"], "a.png", { type: "image/png" });
    await api("/api/x", { method: "POST", file });
    const [, init] = fetchMock.mock.calls[0];
    expect(init?.body).toBe(file);
    expect(init?.headers).toEqual({ "Content-Type": "image/png" });
  });

  it("204 没有内容也不报错", async () => {
    fetchMock.mockResolvedValue(new Response(null, { status: 204 }));
    await expect(api("/api/x")).resolves.toBeUndefined();
  });

  it("网络失败时给出可读的提示", async () => {
    fetchMock.mockRejectedValue(new TypeError("failed"));
    await expect(api("/api/x")).rejects.toMatchObject({ status: 0, message: "无法连接服务器，请检查网络" });
  });

  it.each([
    [409, { detail: "已被别人修改" }, "已被别人修改"],
    [400, { detail: { message: "导入失败" } }, "导入失败"],
    [422, { detail: [{ loc: ["body"], msg: "x" }] }, "填写的内容不符合要求，请检查后重试"],
    [500, null, "请求失败，请稍后再试"],
  ])("%i 错误显示合适的文字", async (status, body, message) => {
    fetchMock.mockResolvedValue(json(status, body));
    await expect(api("/api/x")).rejects.toMatchObject({ status, message });
  });

  it("保留 CSV 导入每行的错误", async () => {
    const errors = [{ line: 2, message: "类型不对" }];
    fetchMock.mockResolvedValue(json(400, { detail: { message: "导入失败", errors } }));
    const error = await api("/api/x").catch((e: ApiError) => e);
    expect((error as ApiError).rowErrors).toEqual(errors);
  });

  it("登录过期时通知应用", async () => {
    fetchMock.mockResolvedValue(json(401, { detail: "请先登录" }));
    await expect(api("/api/tickets")).rejects.toBeInstanceOf(ApiError);
    expect(onUnauthorized).toHaveBeenCalledOnce();
  });

  it("密码输错不算登录过期", async () => {
    fetchMock.mockResolvedValue(json(401, { detail: "账号或密码错误" }));
    await expect(api("/api/auth/login", { method: "POST" })).rejects.toBeInstanceOf(ApiError);
    expect(onUnauthorized).not.toHaveBeenCalled();
  });
});

describe("streamEvents", () => {
  const encode = (text: string) => new TextEncoder().encode(text);

  async function collect(): Promise<[string, unknown][]> {
    const events: [string, unknown][] = [];
    await streamEvents("/api/x", (event, data) => events.push([event, data]));
    return events;
  }

  it("解析多个事件", async () => {
    fetchMock.mockResolvedValue(
      stream([encode('event: delta\ndata: {"text":"a"}\n\nevent: done\ndata: {"ok":true}\n\n')]),
    );
    await expect(collect()).resolves.toEqual([
      ["delta", { text: "a" }],
      ["done", { ok: true }],
    ]);
  });

  it("事件被拆到多个网络包里也能还原", async () => {
    fetchMock.mockResolvedValue(stream([encode("event: delta\nda"), encode('ta: {"text":"a"}\n'), encode("\n")]));
    await expect(collect()).resolves.toEqual([["delta", { text: "a" }]]);
  });

  it("中文被拆到两个网络包里也不乱码", async () => {
    const bytes = encode('event: delta\ndata: {"text":"破损"}\n\n');
    const middle = bytes.indexOf(encode("破")[0]) + 1;
    fetchMock.mockResolvedValue(stream([bytes.slice(0, middle), bytes.slice(middle)]));
    await expect(collect()).resolves.toEqual([["delta", { text: "破损" }]]);
  });

  it("请求被拒绝时不进入流程", async () => {
    fetchMock.mockResolvedValue(json(429, { detail: "次数已达上限" }));
    await expect(collect()).rejects.toMatchObject({ status: 429, message: "次数已达上限" });
  });

  it("登录过期时通知应用", async () => {
    fetchMock.mockResolvedValue(json(401, { detail: "请先登录" }));
    await expect(collect()).rejects.toBeInstanceOf(ApiError);
    expect(onUnauthorized).toHaveBeenCalledOnce();
  });
});
