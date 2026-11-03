import { flushPromises, mount } from "@vue/test-utils";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { api, ApiError, type ClaimBrief } from "../api";
import ClaimBriefCard from "./ClaimBriefCard.vue";

vi.mock("../api", async (original) => ({
  ...(await original<typeof import("../api")>()),
  api: vi.fn(),
}));

const BRIEF: ClaimBrief = {
  source: "model",
  likely_reasons: [{ code: "R05", name: "外包装完好", why: "签收时没有验货", cites: ["T-YD", "C99"] }],
  evidence_gaps: [],
  counter_points: [{ code: "K01", name: "未尽提示义务", why: "条款没有提示", cites: ["L04"] }],
  sources: [
    { id: "T-YD", title: "韵达条款", url: "https://example.com/yd", tier: "官网", version_date: "2025", is_weak: false },
    { id: "L04", title: "民法典", url: "https://example.com/l4", tier: "法规", version_date: "2021", is_weak: true },
  ],
  carrier_terms: {
    title: "韵达赔偿标准",
    source_tier: "第三方转载",
    is_weak_source: false,
    terms: [{ label: "索赔时效", value: "30天" }],
  },
};

async function analyze(brief: Partial<ClaimBrief> = {}) {
  vi.mocked(api).mockResolvedValue({ ...BRIEF, ...brief });
  const wrapper = mount(ClaimBriefCard, { props: { ticketId: "t1" } });
  await wrapper.find("button").trigger("click");
  await flushPromises();
  return wrapper;
}

beforeEach(() => {
  vi.mocked(api).mockReset();
});

describe("索赔依据", () => {
  it("调用接口后显示三组内容和条款", async () => {
    const wrapper = await analyze();
    expect(api).toHaveBeenCalledWith("/api/tickets/t1/ai/claim-brief", { method: "POST" });
    expect(wrapper.text()).toContain("外包装完好：签收时没有验货");
    expect(wrapper.text()).toContain("索赔时效");
    expect(wrapper.text()).toContain("30天");
    expect(wrapper.emitted("finished")).toHaveLength(1);
  });

  it("始终提示只供内部参考", async () => {
    const wrapper = await analyze();
    expect(wrapper.text()).toContain("仅供内部参考，不构成法律意见");
  });

  it("引用链接指向来源，并在新窗口安全打开", async () => {
    const wrapper = await analyze();
    const link = wrapper.find('.cites a[href="https://example.com/yd"]');
    expect(link.text()).toBe("T-YD");
    expect(link.attributes("target")).toBe("_blank");
    expect(link.attributes("rel")).toContain("noopener");
  });

  it("来源列表里没有的引用不显示成链接", async () => {
    const wrapper = await analyze();
    expect(wrapper.findAll(".cites a").map((a) => a.text())).not.toContain("C99");
  });

  it("没有依据的分组如实说明", async () => {
    const wrapper = await analyze();
    expect(wrapper.text()).toContain("资料里没有明确依据");
  });

  it("规则版会标明没有经过模型", async () => {
    expect((await analyze({ source: "rules" })).text()).toContain("这次由规则生成");
    expect((await analyze({ source: "model" })).text()).not.toContain("这次由规则生成");
  });

  it("条款来源较弱时提示核对", async () => {
    const terms = { ...BRIEF.carrier_terms!, is_weak_source: true };
    const wrapper = await analyze({ carrier_terms: terms });
    expect(wrapper.text()).toContain("来源是第三方转载，可能已过时");
  });

  it("较弱的资料在来源列表里提示核对", async () => {
    const wrapper = await analyze();
    const rows = wrapper.findAll("details li");
    expect(rows[0].text()).not.toContain("请核对");
    expect(rows[1].text()).toContain("请核对");
  });

  it("没有这家承运商的条款时不编造", async () => {
    const wrapper = await analyze({ carrier_terms: null });
    expect(wrapper.text()).toContain("语料里没有这家承运商的条款");
    expect(wrapper.find(".terms").exists()).toBe(false);
  });

  it("失败时显示原因，并且可以重试", async () => {
    vi.mocked(api).mockRejectedValue(new ApiError(429, "分析次数已达上限"));
    const wrapper = mount(ClaimBriefCard, { props: { ticketId: "t1" } });
    await wrapper.find("button").trigger("click");
    await flushPromises();
    expect(wrapper.find('[role="alert"]').text()).toBe("分析次数已达上限");
    expect(wrapper.find("button").attributes("disabled")).toBeUndefined();
    expect(wrapper.emitted("finished")).toBeUndefined();
  });
});
