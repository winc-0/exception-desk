<script setup lang="ts">
import { computed, onMounted, ref } from "vue";
import {
  api,
  ApiError,
  CLAIM_STATUS_LABELS,
  REJECTION_LABELS,
  type CarrierSummary,
  type ClaimListItem,
  type ClaimPage,
  type ClaimStatus,
  type ClaimSummary,
} from "../api";
import { formatTime, formatYuan } from "../format";

const summary = ref<ClaimSummary | null>(null);
const claims = ref<ClaimListItem[]>([]);
const nextBefore = ref<number | null>(null);
const statusFilter = ref<ClaimStatus | "">("");
const loading = ref(true);
const loadingMore = ref(false);
const error = ref("");

function recovery(row: CarrierSummary): string {
  // 只看已有结果的索赔。进行中的还不知道能赔多少，算进去会把比例压低
  if (!row.settled_claimed_cents) return "—";
  return `${Math.round((row.paid_cents / row.settled_claimed_cents) * 100)}%`;
}

const tiles = computed(() => {
  const total = summary.value?.total;
  if (!total) return [];
  return [
    { label: "索赔总额", value: formatYuan(total.claimed_cents) },
    { label: "已到账", value: formatYuan(total.paid_cents) },
    { label: "到账比例", value: recovery(total), note: "到账金额 ÷ 已有结果的索赔金额" },
    { label: "进行中", value: `${total.open} 笔` },
  ];
});

function query(before?: number) {
  const params = new URLSearchParams();
  if (statusFilter.value) params.set("status", statusFilter.value);
  if (before) params.set("before", String(before));
  const text = params.toString();
  return text ? `?${text}` : "";
}

let latestRequest = 0;

async function loadList() {
  const request = ++latestRequest;
  error.value = "";
  try {
    const page = await api<ClaimPage>(`/api/claims${query()}`);
    if (request !== latestRequest) return;
    claims.value = page.items;
    nextBefore.value = page.next_before;
  } catch (e) {
    if (request === latestRequest) error.value = e instanceof ApiError ? e.message : "加载失败";
  }
}

async function loadMore() {
  if (nextBefore.value === null) return;
  const request = latestRequest;
  loadingMore.value = true;
  try {
    const page = await api<ClaimPage>(`/api/claims${query(nextBefore.value)}`);
    if (request !== latestRequest) return;
    claims.value.push(...page.items);
    nextBefore.value = page.next_before;
  } catch (e) {
    error.value = e instanceof ApiError ? e.message : "加载失败";
  } finally {
    loadingMore.value = false;
  }
}

onMounted(async () => {
  try {
    summary.value = await api<ClaimSummary>("/api/claims/summary");
  } catch (e) {
    error.value = e instanceof ApiError ? e.message : "加载失败";
  }
  await loadList();
  loading.value = false;
});
</script>

<template>
  <div class="heading"><h2>索赔</h2></div>
  <p v-if="error" class="error" role="alert">{{ error }}</p>
  <p v-if="loading" class="muted">加载中…</p>

  <template v-else>
    <div v-if="summary?.carriers.length" class="tiles">
      <div v-for="tile in tiles" :key="tile.label" class="card tile">
        <span class="muted">{{ tile.label }}</span>
        <strong>{{ tile.value }}</strong>
        <small v-if="tile.note" class="muted">{{ tile.note }}</small>
      </div>
    </div>

    <div v-if="summary?.carriers.length" class="table-wrap section">
      <table>
        <thead>
          <tr>
            <th>承运商</th>
            <th>笔数</th>
            <th>进行中</th>
            <th>被拒</th>
            <th>到账</th>
            <th>索赔金额</th>
            <th>到账金额</th>
            <th>到账比例</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="row in summary.carriers" :key="row.carrier">
            <td>{{ row.carrier }}</td>
            <td>{{ row.total }}</td>
            <td>{{ row.open }}</td>
            <td>{{ row.rejected }}</td>
            <td>{{ row.paid }}</td>
            <td class="nowrap">{{ formatYuan(row.claimed_cents) }}</td>
            <td class="nowrap">{{ formatYuan(row.paid_cents) }}</td>
            <td>{{ recovery(row) }}</td>
          </tr>
        </tbody>
      </table>
    </div>

    <p v-if="summary?.rejections.length" class="muted section">
      当前被拒赔的原因：
      <template v-for="(item, index) in summary.rejections" :key="item.code">
        {{ index ? "、" : "" }}{{ REJECTION_LABELS[item.code] }} {{ item.count }} 笔
      </template>
    </p>

    <div class="filters">
      <select v-model="statusFilter" aria-label="按索赔状态筛选" @change="loadList">
        <option value="">全部状态</option>
        <option v-for="(label, value) in CLAIM_STATUS_LABELS" :key="value" :value="value">{{ label }}</option>
      </select>
    </div>

    <p v-if="!claims.length && !error" class="empty">没有符合条件的索赔。可以在工单详情页发起。</p>
    <template v-else-if="claims.length">
      <div class="table-wrap">
        <table>
          <thead>
            <tr>
              <th>工单号</th>
              <th>状态</th>
              <th>标题</th>
              <th>承运商</th>
              <th>索赔金额</th>
              <th>到账金额</th>
              <th>更新时间</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="claim in claims" :key="claim.ticket_id">
              <td class="mono">#{{ claim.ticket_number }}</td>
              <td class="nowrap">
                <span class="status" :class="`claim-${claim.status}`">{{ CLAIM_STATUS_LABELS[claim.status] }}</span>
              </td>
              <td>
                <RouterLink class="title-link" :to="`/tickets/${claim.ticket_id}`">{{ claim.ticket_title }}</RouterLink>
              </td>
              <td class="nowrap">{{ claim.carrier }}</td>
              <td class="nowrap">{{ formatYuan(claim.claim_amount_cents) }}</td>
              <td class="nowrap">{{ formatYuan(claim.paid_cents) }}</td>
              <td class="nowrap">{{ formatTime(claim.updated_at) }}</td>
            </tr>
          </tbody>
        </table>
      </div>
      <div v-if="nextBefore !== null" class="load-more">
        <button class="secondary" type="button" :disabled="loadingMore" @click="loadMore">
          {{ loadingMore ? "加载中…" : "加载更多" }}
        </button>
      </div>
    </template>
  </template>
</template>
