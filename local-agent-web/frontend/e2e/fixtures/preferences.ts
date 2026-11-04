import type { PreferencesDto } from '../../src/api/client'
import { generalDefaults } from '../../src/features/settings/preferencesContext'

export function preferencesFixture(): PreferencesDto {
  const values = {
    general: { ...generalDefaults },
    execution: { max_tool_loops: 20, max_retries_per_step: 2, shell_timeout_seconds: 60, shell_max_output_chars: 30000, safe_delete: true },
    // 项目规则（2026-10-26）：三项从"高级只读"搬进独立分区。
    agents: { enabled: true, max_file_chars: 20000, max_total_chars: 60000 },
    retrieval: { enabled: true, max_files: 2000, max_results: 8, timeout_seconds: 10, context_lines: 20 },
    web_access: { provider: 'auto' as const, fetch_provider: 'local' as const, max_results: 5, max_chars: 8000, prefetch_pages: 2, max_search_calls: 4, max_read_pages: 6, max_reads_per_page: 3, timeout_seconds: 15, domain_policy: 'allow' as const, denied_domains: [], allowed_domains: [] },
    memory: { enabled: true, confidence_threshold: .75, importance_threshold: .5, aging_enabled: true, forget_after_days: 180 },
    medium_term: { enabled: false, top_k: 8, min_score: .53, decay_half_life_days: 30 },
    consolidation: { enabled: false, interval_hours: 24, max_promotions: 5 },
    // 压缩（2026-10-26）：从"高级只读"搬进独立分区，与后端 `CompressionPreferences` 同形。
    compression: { enabled: true, trigger_ratio: .8, preserve_recent_turns: 8, retain_ratio: .16 },
    embedding: { enabled: false, provider: 'onnx_local' as const, model: 'unit-test', cache_dir: 'E:/models', local_files_only: true, base_url: '', api_key_env: '', allow_plaintext_http: false },
  }
  // `advanced` 只剩内部预算（压缩的开关/比例/保留轮数已搬进 `compression` 组，2026-10-26）。
  return { version: 0, writable: true, values, defaults: structuredClone(values), advanced: { tool_step_budget_chars: 160000 } }
}
