import { defineStore } from 'pinia'
import { ref } from 'vue'
import { citiesApi } from '@/api/cities'

export const useCitiesStore = defineStore('cities', () => {
  // 已点亮城市 adcode 集合
  const lit = ref<Set<string>>(new Set())
  const loading = ref(false)

  async function fetchLit() {
    loading.value = true
    try {
      const res = await citiesApi.getLighted()
      lit.value = new Set(res.data.adcode || [])
    } finally {
      loading.value = false
    }
  }

  async function light(adcode: string) {
    await citiesApi.light(adcode)
    lit.value = new Set(lit.value).add(adcode)
  }

  async function unlight(adcode: string) {
    await citiesApi.unlight(adcode)
    const next = new Set(lit.value)
    next.delete(adcode)
    lit.value = next
  }

  function isLit(adcode: string) {
    return lit.value.has(adcode)
  }

  return { lit, loading, fetchLit, light, unlight, isLit }
})
