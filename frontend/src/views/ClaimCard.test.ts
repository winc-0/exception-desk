import { flushPromises, mount } from "@vue/test-utils";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { api, ApiError, type Claim, type TicketDetail } from "../api";
import ClaimCard from "./ClaimCard.vue";

vi.mock("../api", async (original) => ({
  ...(await original<typeof import("../api")>()),
  api: vi.fn(),
}));

function claim(fields: Partial<Claim> = {}): Claim {
  return {
    version: 3,
    status: "preparing",
    carrier: "韵达",
    claim_amount_cents: 1999,
    approved_cents: null,
    paid_cents: null,
    insured: false,
    pickup_weight_g: null,
    billed_weight_g: null,
    external_ref: "",
    rejection_reason: "",
    rejection_code: null,
    goods_quantity: null,
    created_at: "2026-11-01T00:00:00Z",
    updated_at: "2026-11-01T00:00:00Z",
    submitted_at: null,
    paid_at: null,
    ...fields,
  };
}

function ticket(fields: Partial<TicketDetail> = {}): TicketDetail {
  return {
    id: "t1",
    claim: null,
    can_upload: true,
    claim_transitions: [],
    evidence_checklist: [],
    ...fields,
  } as TicketDetail;
}

function render(fields: Partial<TicketDetail> = {}) {
  return mount(ClaimCard, { props: { ticket: ticket(fields) } });
}

type Wrapper = ReturnType<typeof render>;

function button(wrapper: Wrapper, text: string) {
  return wrapper.findAll("button").find((b) => b.text() === text)!;
}

function field(wrapper: Wrapper, label: string) {
  return wrapper
    .findAll("label")
    .find((l) => l.text().startsWith(label))!
    .find("input, textarea, select");
}

async function submit(wrapper: Wrapper) {
  await wrapper.find("form").trigger("submit");
  await flushPromises();
}

beforeEach(() => {
  vi.mocked(api).mockReset().mockResolvedValue(ticket());
});

describe("填写索赔信息", () => {
  async function open() {
    const wrapper = render();
    await button(wrapper, "发起索赔").trigger("click");
    await field(wrapper, "承运商").setValue("韵达");
    return wrapper;
  }

  it("金额和重量按整数提交", async () => {
    const wrapper = await open();
    await field(wrapper, "索赔金额").setValue("19.99");
    await field(wrapper, "揽收重量").setValue("1.25");
    await field(wrapper, "货物数量").setValue("3");
    await submit(wrapper);
    expect(api).toHaveBeenCalledWith("/api/tickets/t1/claim", {
      method: "POST",
      body: {
        carrier: "韵达",
        goods_quantity: 3,
        claim_amount_cents: 1999,
        insured: false,
        pickup_weight_g: 1250,
        billed_weight_g: null,
      },
    });
    expect(wrapper.emitted("updated")).toHaveLength(1);
  });

  it.each([
    ["索赔金额", "abc", "索赔金额格式不对"],
    ["揽收重量", "1.2345", "重量格式不对"],
    ["货物数量", "1.5", "货物数量要填正整数"],
  ])("%s 填 %s 时不提交", async (label, value, message) => {
    const wrapper = await open();
    await field(wrapper, label).setValue(value);
    await submit(wrapper);
    expect(api).not.toHaveBeenCalled();
    expect(wrapper.find('[role="alert"]').text()).toContain(message);
  });

  it("修改时带版本号，并回填原来的金额", async () => {
    const wrapper = render({ claim: claim() });
    await button(wrapper, "修改信息").trigger("click");
    expect((field(wrapper, "索赔金额").element as HTMLInputElement).value).toBe("19.99");
    await submit(wrapper);
    const [path, options] = vi.mocked(api).mock.calls[0];
    expect(path).toBe("/api/tickets/t1/claim");
    expect(options?.method).toBe("PATCH");
    expect((options?.body as { version: number }).version).toBe(3);
  });

  it("没有上传权限时不能发起", () => {
    expect(render({ can_upload: false }).text()).not.toContain("发起索赔");
  });
});

describe("推进状态", () => {
  it("只显示服务端允许的操作", () => {
    const wrapper = render({ claim: claim(), claim_transitions: ["submitted"] });
    expect(button(wrapper, "提交索赔")).toBeDefined();
    expect(wrapper.text()).not.toContain("确认到账");
  });

  it("到账金额按分提交，并带版本号", async () => {
    const wrapper = render({ claim: claim({ status: "approved" }), claim_transitions: ["paid"] });
    await button(wrapper, "确认到账").trigger("click");
    await field(wrapper, "到账金额").setValue("128.50");
    await submit(wrapper);
    expect(api).toHaveBeenCalledWith("/api/tickets/t1/claim/transition", {
      method: "POST",
      body: { to: "paid", version: 3, note: "", paid_cents: 12850 },
    });
  });

  it("金额不对时不提交", async () => {
    const wrapper = render({ claim: claim({ status: "submitted" }), claim_transitions: ["approved"] });
    await button(wrapper, "承运商已核定").trigger("click");
    await field(wrapper, "核定金额").setValue("abc");
    await submit(wrapper);
    expect(api).not.toHaveBeenCalled();
    expect(wrapper.find('[role="alert"]').text()).toContain("请填写正确的金额");
  });

  it("拒赔必须选原因，选“其他”还要写说明", async () => {
    const wrapper = render({ claim: claim({ status: "submitted" }), claim_transitions: ["rejected"] });
    await button(wrapper, "承运商已拒赔").trigger("click");
    const confirm = () => button(wrapper, "确认").attributes("disabled");
    expect(confirm()).toBeDefined();
    await field(wrapper, "拒赔原因").setValue("other");
    expect(confirm()).toBeDefined();
    await field(wrapper, "补充说明").setValue("对方口头拒绝");
    expect(confirm()).toBeUndefined();
    await field(wrapper, "拒赔原因").setValue("uninsured");
    await submit(wrapper);
    expect(vi.mocked(api).mock.calls[0][1]?.body).toMatchObject({
      to: "rejected",
      rejection_code: "uninsured",
    });
  });

  it("被拒后再次提交必须说明补了什么", async () => {
    const wrapper = render({ claim: claim({ status: "rejected" }), claim_transitions: ["submitted"] });
    await button(wrapper, "再次提交").trigger("click");
    expect(button(wrapper, "确认").attributes("disabled")).toBeDefined();
    await field(wrapper, "补充了什么").setValue("补了发票");
    expect(button(wrapper, "确认").attributes("disabled")).toBeUndefined();
  });

  it("上一步填的内容不会带到下一步", async () => {
    const wrapper = render({ claim: claim({ status: "submitted" }), claim_transitions: ["rejected", "withdrawn"] });
    await button(wrapper, "承运商已拒赔").trigger("click");
    await field(wrapper, "补充说明").setValue("这段不该留下");
    await button(wrapper, "撤回索赔").trigger("click");
    expect((field(wrapper, "原因").element as HTMLTextAreaElement).value).toBe("");
  });

  it("别人刚改过时通知刷新", async () => {
    vi.mocked(api).mockRejectedValue(new ApiError(409, "内容已更新"));
    const wrapper = render({ claim: claim({ status: "approved" }), claim_transitions: ["paid"] });
    await button(wrapper, "确认到账").trigger("click");
    await field(wrapper, "到账金额").setValue("1");
    await submit(wrapper);
    expect(wrapper.emitted("stale")).toHaveLength(1);
    expect(wrapper.find('[role="alert"]').text()).toBe("内容已更新");
  });

  it("其他错误不触发刷新", async () => {
    vi.mocked(api).mockRejectedValue(new ApiError(422, "必备证据不齐"));
    const wrapper = render({ claim: claim({ status: "approved" }), claim_transitions: ["paid"] });
    await button(wrapper, "确认到账").trigger("click");
    await field(wrapper, "到账金额").setValue("1");
    await submit(wrapper);
    expect(wrapper.emitted("stale")).toBeUndefined();
  });
});

it("准备中缺必备证据时提示", () => {
  const evidence_checklist = [
    { category: "packing_video", label: "打包视频", level: "required", count: 0 },
    { category: "value_proof", label: "价值证明", level: "required", count: 1 },
    { category: "weight_record", label: "称重记录", level: "recommended", count: 0 },
  ] as TicketDetail["evidence_checklist"];
  const wrapper = render({ claim: claim(), evidence_checklist });
  expect(wrapper.text()).toContain("提交前还缺：打包视频");
  expect(wrapper.text()).not.toContain("价值证明");
  expect(wrapper.text()).not.toContain("称重记录");
});
