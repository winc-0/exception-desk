<script setup lang="ts">
import { computed, ref } from "vue";
import { api, ApiError, type BriefSource, type ClaimBrief } from "../api";

const props = defineProps<{ ticketId: string }>();
const emit = defineEmits<{ finished: [] }>();

const brief = ref<ClaimBrief | null>(null);
const loading = ref(false);
const error = ref("");

const sources = computed(() => new Map((brief.value?.sources ?? []).map((s) => [s.id, s])));
const groups = computed(() => {
  const value = brief.value;
  if (!value) return [];
  return [
    { title: "承运商可能的拒赔理由", items: value.likely_reasons },
    { title: "还缺的证据", items: value.evidence_gaps },
    { title: "可用的反驳", items: value.counter_points },
  ];
});

function citation(id: string): BriefSource | undefined {
  return sources.value.get(id);
}

async function generate() {
  loading.value = true;
  error.value = "";
  try {
    brief.value = await api<ClaimBrief>(`/api/tickets/${props.ticketId}/ai/claim-brief`, { method: "POST" });
    emit("finished");
  } catch (e) {
    error.value = e instanceof ApiError ? e.message : "生成失败，请稍后再试";
  } finally {
    loading.value = false;
  }
}
</script>

<template>
  <div class="card ai-draft brief">
    <div class="card-head">
      <h3>索赔依据</h3>
      <button class="secondary" type="button" :disabled="loading" @click="generate">
        {{ loading ? "分析中…" : brief ? "重新分析" : "分析依据" }}
      </button>
    </div>
    <p v-if="!brief" class="muted">根据工单情况，查找相关法规、承运商条款和判例，列出可能的拒赔理由和证据缺口。</p>
    <p v-if="error" class="error" role="alert">{{ error }}</p>

    <template v-if="brief">
      <p class="muted">
        仅供内部参考，不构成法律意见。法院判决两边都有，以运单实际条款和个案情况为准。
        <template v-if="brief.source === 'rules'">这次由规则生成，没有经过模型。</template>
      </p>

      <div v-if="brief.carrier_terms" class="terms">
        <strong>{{ brief.carrier_terms.title }}</strong>
        <p v-if="brief.carrier_terms.is_weak_source" class="hint-inline">
          来源是{{ brief.carrier_terms.source_tier }}，可能已过时，提交前请核对最新条款。
        </p>
        <dl class="claim-facts">
          <div v-for="term in brief.carrier_terms.terms" :key="term.label">
            <dt>{{ term.label }}</dt>
            <dd>{{ term.value }}</dd>
          </div>
        </dl>
      </div>
      <p v-else class="hint-inline">语料里没有这家承运商的条款，限额和时效请以运单条款为准。</p>

      <section v-for="group in groups" :key="group.title" class="brief-group">
        <h4>{{ group.title }}</h4>
        <p v-if="!group.items.length" class="muted">资料里没有明确依据。</p>
        <ul v-else>
          <li v-for="item in group.items" :key="item.code">
            <strong>{{ item.name }}</strong>：{{ item.why }}
            <span class="cites">
              <template v-for="id in item.cites" :key="id">
                <a v-if="citation(id)" :href="citation(id)!.url" target="_blank" rel="noopener noreferrer" :title="citation(id)!.title">
                  {{ id }}
                </a>
              </template>
            </span>
          </li>
        </ul>
      </section>

      <details class="brief-group">
        <summary>查看 {{ brief.sources.length }} 份资料来源</summary>
        <ul>
          <li v-for="source in brief.sources" :key="source.id">
            <a :href="source.url" target="_blank" rel="noopener noreferrer">{{ source.id }} {{ source.title }}</a>
            <span class="muted">
              {{ source.tier }} · {{ source.version_date }}<template v-if="source.is_weak"> · 请核对</template>
            </span>
          </li>
        </ul>
      </details>
    </template>
  </div>
</template>
