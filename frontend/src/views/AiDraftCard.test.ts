import { flushPromises, mount } from "@vue/test-utils";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { api, ApiError, streamEvents } from "../api";
import AiDraftCard from "./AiDraftCard.vue";

vi.mock("../api", async (original) => ({
  ...(await original<typeof import("../api")>()),
  api: vi.fn(),
  streamEvents: vi.fn(),
}));

type Send = (event: string, data: Record<string, unknown>) => void;
type Wrapper = ReturnType<typeof mount<typeof AiDraftCard>>;
const DONE = { draft: "校验后的内容", draft_id: "d1", source: "model", reason: "" };

let send: Send;
let finish: () => void;
const writeText = vi.fn<(text: string) => Promise<void>>();

function button(wrapper: Wrapper, text: string) {
  return wrapper.findAll("button").find((b) => b.text() === text);
}

function boxValue(wrapper: Wrapper) {
  return (wrapper.find("textarea").element as HTMLTextAreaElement).value;
}

/** 点击生成，流保持打开，由测试决定发送什么事件。 */
async function start() {
  vi.mocked(streamEvents).mockImplementation((_path, onEvent) => {
    send = onEvent;
    return new Promise((resolve) => {
      finish = resolve;
    });
  });
  const wrapper = mount(AiDraftCard, { props: { ticketId: "t1" } });
  await button(wrapper, "生成草稿")!.trigger("click");
  return wrapper;
}

async function finished() {
  const wrapper = await start();
  send("done", DONE);
  finish();
  await flushPromises();
  return wrapper;
}

beforeEach(() => {
  vi.mocked(api).mockReset().mockResolvedValue(undefined);
  vi.mocked(streamEvents).mockReset();
  writeText.mockReset().mockResolvedValue(undefined);
  Object.defineProperty(navigator, "clipboard", { value: { writeText }, configurable: true });
});

describe("校验前", () => {
  it("文字可以看到，但不能修改和复制", async () => {
    const wrapper = await start();
    send("delta", { text: "您好，" });
    send("delta", { text: "正在处理" });
    await flushPromises();
    expect(boxValue(wrapper)).toBe("您好，正在处理");
    expect(wrapper.find("textarea").attributes("readonly")).toBeDefined();
    expect(button(wrapper, "复制")).toBeUndefined();
    expect(wrapper.text()).toContain("校验通过前不要使用");
  });

  it("校验不通过重写时清掉旧内容", async () => {
    const wrapper = await start();
    send("delta", { text: "不合格的内容" });
    send("reset", {});
    await flushPromises();
    expect(boxValue(wrapper)).toBe("");
    expect(wrapper.text()).toContain("正在重写");
  });

  it("显示风险提示和正在读取的内容", async () => {
    const wrapper = await start();
    send("facts", { missing_required: [], risks: ["索赔时效快到了"] });
    send("tool", { names: ["get_ticket", "get_claim"] });
    await flushPromises();
    expect(wrapper.text()).toContain("索赔时效快到了");
    expect(wrapper.text()).toContain("工单信息、索赔情况");
  });

  it("连接中断时不把半截内容当成结果", async () => {
    const wrapper = await start();
    send("delta", { text: "半截" });
    finish();
    await flushPromises();
    expect(wrapper.find('[role="alert"]').text()).toContain("连接中断");
    expect(button(wrapper, "复制")).toBeUndefined();
  });
});

describe("校验后", () => {
  it("以校验后的结果为准，不用流式内容", async () => {
    const wrapper = await start();
    send("delta", { text: "流式内容" });
    send("done", DONE);
    finish();
    await flushPromises();
    expect(boxValue(wrapper)).toBe("校验后的内容");
    expect(wrapper.find("textarea").attributes("readonly")).toBeUndefined();
    expect(wrapper.emitted("finished")).toHaveLength(1);
  });

  it("直接复制记为 used", async () => {
    const wrapper = await finished();
    await button(wrapper, "复制")!.trigger("click");
    await flushPromises();
    expect(writeText).toHaveBeenCalledWith("校验后的内容");
    expect(api).toHaveBeenCalledWith("/api/tickets/t1/ai/reply-draft/d1/feedback", {
      method: "POST",
      body: { verdict: "used" },
    });
  });

  it("改过再复制记为 edited，复制的是改过的内容", async () => {
    const wrapper = await finished();
    await wrapper.find("textarea").setValue("客服改过的内容");
    await button(wrapper, "复制")!.trigger("click");
    await flushPromises();
    expect(writeText).toHaveBeenCalledWith("客服改过的内容");
    expect(vi.mocked(api).mock.calls[0][1]?.body).toEqual({ verdict: "edited" });
  });

  it("复制失败时提示，不记为使用", async () => {
    writeText.mockRejectedValue(new Error("denied"));
    const wrapper = await finished();
    await button(wrapper, "复制")!.trigger("click");
    await flushPromises();
    expect(wrapper.find('[role="alert"]').text()).toContain("复制失败");
    expect(api).not.toHaveBeenCalled();
  });

  it("不用这份时记为 rejected 并清空", async () => {
    const wrapper = await finished();
    await button(wrapper, "不用这份")!.trigger("click");
    await flushPromises();
    expect(vi.mocked(api).mock.calls[0][1]?.body).toEqual({ verdict: "rejected" });
    expect(wrapper.find("textarea").exists()).toBe(false);
  });

  it("反馈失败不打扰客服", async () => {
    vi.mocked(api).mockRejectedValue(new Error("down"));
    const wrapper = await finished();
    await button(wrapper, "复制")!.trigger("click");
    await flushPromises();
    expect(wrapper.find('[role="alert"]').exists()).toBe(false);
    expect(wrapper.text()).toContain("已复制");
  });

  it("模板内容会标明没有经过模型", async () => {
    const wrapper = await start();
    send("done", { ...DONE, source: "template", reason: "校验未通过" });
    finish();
    await flushPromises();
    expect(wrapper.text()).toContain("这是模板内容，没有经过模型");
    expect(wrapper.text()).toContain("未通过校验或调用失败");
  });
});

it("服务端拒绝时显示原因，并且可以重试", async () => {
  vi.mocked(streamEvents).mockRejectedValue(new ApiError(429, "次数已达上限"));
  const wrapper = mount(AiDraftCard, { props: { ticketId: "t1" } });
  await button(wrapper, "生成草稿")!.trigger("click");
  await flushPromises();
  expect(wrapper.find('[role="alert"]').text()).toBe("次数已达上限");
  expect(button(wrapper, "生成草稿")!.attributes("disabled")).toBeUndefined();
});
