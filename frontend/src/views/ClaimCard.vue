<script setup lang="ts">
import { computed, reactive, ref } from "vue";
import {
  api,
  ApiError,
  CLAIM_STATUS_LABELS,
  REJECTION_LABELS,
  type ClaimStatus,
  type RejectionCode,
  type TicketDetail,
} from "../api";
import { formatMinor, formatTime, formatYuan, parseMinor } from "../format";

const props = defineProps<{ ticket: TicketDetail }>();
const emit = defineEmits<{ updated: [ticket: TicketDetail]; stale: [] }>();

const claim = computed(() => props.ticket.claim);
const error = ref("");
const busy = ref(false);

const editing = ref(false);
const form = reactive({ carrier: "", quantity: "", amount: "", insured: false, pickup: "", billed: "" });

const pending = ref<ClaimStatus | null>(null);
const action = reactive({ note: "", amount: "", externalRef: "", rejectionCode: "" as RejectionCode | "" });

const missingEvidence = computed(() =>
  props.ticket.evidence_checklist.filter((i) => i.level === "required" && !i.count).map((i) => i.label),
);

const weightGap = computed(() => {
  const value = claim.value;
  if (!value?.pickup_weight_g || !value.billed_weight_g) return null;
  return value.pickup_weight_g - value.billed_weight_g;
});

function actionLabel(from: ClaimStatus, to: ClaimStatus): string {
  if (from === "rejected" && to === "submitted") return "再次提交";
  return {
    preparing: "重新准备",
    submitted: "提交索赔",
    approved: "承运商已核定",
    rejected: "承运商已拒赔",
    paid: "确认到账",
    withdrawn: "撤回索赔",
  }[to];
}

const notePrompt = computed(() =>
  pending.value === "rejected" ? "补充说明（选“其他”时必填）" : pending.value === "submitted" ? "补充了什么（再次提交时必填）" : "原因",
);
const noteRequired = computed(() => {
  if (pending.value === "submitted") return claim.value?.status === "rejected";
  if (pending.value === "rejected") return action.rejectionCode === "other";
  return pending.value === "withdrawn";
});

function startEdit() {
  const value = claim.value;
  form.carrier = value?.carrier ?? "";
  form.quantity = value?.goods_quantity ? String(value.goods_quantity) : "";
  form.amount = formatMinor(value?.claim_amount_cents ?? null, 2);
  form.insured = value?.insured ?? false;
  form.pickup = formatMinor(value?.pickup_weight_g ?? null, 3);
  form.billed = formatMinor(value?.billed_weight_g ?? null, 3);
  error.value = "";
  editing.value = true;
}

function fail(e: unknown) {
  error.value = e instanceof ApiError ? e.message : "操作失败，请稍后再试";
  if (e instanceof ApiError && e.status === 409) emit("stale");
}

async function saveForm() {
  const amount = parseMinor(form.amount, 2);
  const pickup = parseMinor(form.pickup, 3);
  const billed = parseMinor(form.billed, 3);
  if (form.quantity && !/^[1-9]\d{0,5}$/.test(form.quantity)) return void (error.value = "货物数量要填正整数");
  if (amount === undefined) return void (error.value = "索赔金额格式不对，例如 128.50");
  if (pickup === undefined || billed === undefined) return void (error.value = "重量格式不对，例如 1.25");

  const body = {
    carrier: form.carrier,
    goods_quantity: form.quantity ? Number(form.quantity) : null,
    claim_amount_cents: amount,
    insured: form.insured,
    pickup_weight_g: pickup,
    billed_weight_g: billed,
  };
  error.value = "";
  busy.value = true;
  try {
    const path = `/api/tickets/${props.ticket.id}/claim`;
    const updated = claim.value
      ? await api<TicketDetail>(path, { method: "PATCH", body: { ...body, version: claim.value.version } })
      : await api<TicketDetail>(path, { method: "POST", body });
    emit("updated", updated);
    editing.value = false;
  } catch (e) {
    fail(e);
  } finally {
    busy.value = false;
  }
}

function choose(to: ClaimStatus) {
  // 每次都清空，上一步填的内容不能带到下一步的审计记录里
  action.note = "";
  action.amount = "";
  action.externalRef = claim.value?.external_ref ?? "";
  action.rejectionCode = "";
  error.value = "";
  // 只有“重新准备”不用多填内容，点击后直接执行
  if (to === "preparing") {
    pending.value = null;
    void move(to);
  } else {
    pending.value = to;
  }
}

async function move(to: ClaimStatus) {
  const body: Record<string, unknown> = { to, version: claim.value!.version, note: action.note };
  if (to === "submitted") body.external_ref = action.externalRef;
  if (to === "rejected") body.rejection_code = action.rejectionCode;
  if (to === "approved" || to === "paid") {
    const amount = parseMinor(action.amount, 2);
    if (!amount) return void (error.value = "请填写正确的金额，例如 128.50");
    body[to === "approved" ? "approved_cents" : "paid_cents"] = amount;
  }
  error.value = "";
  busy.value = true;
  try {
    emit(
      "updated",
      await api<TicketDetail>(`/api/tickets/${props.ticket.id}/claim/transition`, { method: "POST", body }),
    );
    pending.value = null;
  } catch (e) {
    fail(e);
  } finally {
    busy.value = false;
  }
}
</script>

<template>
  <div class="card claim">
    <div class="card-head">
      <h3>承运商索赔</h3>
      <span v-if="claim" class="status" :class="`claim-${claim.status}`">{{ CLAIM_STATUS_LABELS[claim.status] }}</span>
    </div>

    <template v-if="claim && !editing">
      <dl class="claim-facts">
        <div>
          <dt>承运商</dt>
          <dd>{{ claim.carrier }}{{ claim.insured ? "（已保价）" : "（未保价）" }}</dd>
        </div>
        <div>
          <dt>货物数量</dt>
          <dd>{{ claim.goods_quantity ?? "—" }}</dd>
        </div>
        <div>
          <dt>索赔金额</dt>
          <dd>{{ formatYuan(claim.claim_amount_cents) }}</dd>
        </div>
        <div v-if="claim.approved_cents">
          <dt>核定金额</dt>
          <dd>{{ formatYuan(claim.approved_cents) }}</dd>
        </div>
        <div v-if="claim.paid_cents">
          <dt>到账金额</dt>
          <dd>{{ formatYuan(claim.paid_cents) }}</dd>
        </div>
        <div>
          <dt>揽收重量</dt>
          <dd>{{ claim.pickup_weight_g ? `${formatMinor(claim.pickup_weight_g, 3)} kg` : "—" }}</dd>
        </div>
        <div>
          <dt>计费重量</dt>
          <dd>{{ claim.billed_weight_g ? `${formatMinor(claim.billed_weight_g, 3)} kg` : "—" }}</dd>
        </div>
        <div v-if="claim.external_ref">
          <dt>理赔单号</dt>
          <dd class="mono">{{ claim.external_ref }}</dd>
        </div>
        <div v-if="claim.submitted_at">
          <dt>提交时间</dt>
          <dd>{{ formatTime(claim.submitted_at) }}</dd>
        </div>
      </dl>
      <p v-if="weightGap" class="hint-inline">
        揽收重量比计费重量{{ weightGap > 0 ? "多" : "少" }} {{ formatMinor(Math.abs(weightGap), 3) }} kg。
      </p>
      <p v-if="claim.status === 'rejected'" class="error">
        拒赔原因：{{ claim.rejection_code ? REJECTION_LABELS[claim.rejection_code] : "" }}
        <template v-if="claim.rejection_reason">（{{ claim.rejection_reason }}）</template>
      </p>
      <p v-if="claim.status === 'preparing' && missingEvidence.length" class="hint-inline">
        提交前还缺：{{ missingEvidence.join("、") }}
      </p>

      <div class="actions">
        <button
          v-for="to in ticket.claim_transitions"
          :key="to"
          :class="to === 'withdrawn' ? 'danger' : 'secondary'"
          type="button"
          :disabled="busy"
          @click="choose(to)"
        >
          {{ actionLabel(claim.status, to) }}
        </button>
        <button
          v-if="claim.status === 'preparing' || claim.status === 'rejected'"
          class="ghost"
          type="button"
          @click="startEdit"
        >
          修改信息
        </button>
      </div>

      <form v-if="pending" class="pending" @submit.prevent="move(pending!)">
        <label v-if="pending === 'approved' || pending === 'paid'">
          {{ pending === "approved" ? "核定金额（元）" : "到账金额（元）" }}
          <input v-model.trim="action.amount" inputmode="decimal" required />
        </label>
        <label v-if="pending === 'rejected'">
          拒赔原因
          <select v-model="action.rejectionCode" required>
            <option value="" disabled>请选择</option>
            <option v-for="(label, value) in REJECTION_LABELS" :key="value" :value="value">{{ label }}</option>
          </select>
        </label>
        <label v-if="pending === 'submitted'">
          承运商理赔单号（选填）
          <input v-model.trim="action.externalRef" maxlength="100" />
        </label>
        <label v-if="pending !== 'approved' && pending !== 'paid'">
          {{ notePrompt }}
          <textarea v-model="action.note" rows="2" :required="noteRequired" maxlength="2000"></textarea>
        </label>
        <div class="actions">
          <button
            type="submit"
            :disabled="busy || (noteRequired && !action.note.trim()) || (pending === 'rejected' && !action.rejectionCode)"
          >
            确认
          </button>
          <button class="ghost" type="button" @click="pending = null">放弃</button>
        </div>
      </form>
    </template>

    <p v-else-if="!claim && !editing" class="muted">这张工单还没有向承运商索赔。</p>

    <form v-if="editing" class="form-grid claim-form" @submit.prevent="saveForm">
      <label>
        承运商
        <input v-model.trim="form.carrier" required maxlength="64" />
      </label>
      <label>
        货物数量（件）
        <input v-model.trim="form.quantity" inputmode="numeric" />
      </label>
      <label>
        索赔金额（元）
        <input v-model.trim="form.amount" inputmode="decimal" placeholder="提交前必填" />
      </label>
      <label>
        揽收重量（kg，选填）
        <input v-model.trim="form.pickup" inputmode="decimal" />
      </label>
      <label>
        计费重量（kg，选填）
        <input v-model.trim="form.billed" inputmode="decimal" />
      </label>
      <label class="wide check">
        <input v-model="form.insured" type="checkbox" />
        这票货已保价
      </label>
      <div class="wide actions">
        <button type="submit" :disabled="busy || !form.carrier">{{ claim ? "保存" : "开始准备索赔" }}</button>
        <button class="ghost" type="button" @click="editing = false">放弃</button>
      </div>
    </form>

    <p v-if="error" class="error" role="alert">{{ error }}</p>

    <div v-if="!claim && !editing && ticket.can_upload">
      <button class="secondary" type="button" @click="startEdit">发起索赔</button>
    </div>
  </div>
</template>
