import { onUnmounted, ref, shallowRef } from 'vue'

export function useReadonly<T>() {
  const data = shallowRef<T | null>(null), busy = ref(false), error = ref('')
  let revision = 0
  function clear() { revision++; data.value = null; error.value = ''; busy.value = false }
  async function load(fetcher: () => Promise<T>) {
    const current = ++revision; data.value = null; busy.value = true; error.value = ''
    try { const result = await fetcher(); if (current === revision) data.value = result }
    catch (e) { if (current === revision) error.value = e instanceof Error ? e.message : '查询失败，请重试' }
    finally { if (current === revision) busy.value = false }
  }
  onUnmounted(clear)
  return { data, busy, error, load, clear }
}
