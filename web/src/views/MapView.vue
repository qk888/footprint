<script setup lang="ts">
import { ref, onMounted, onActivated, onBeforeUnmount, nextTick } from 'vue'
import { useRouter } from 'vue-router'
import { useMessage, useDialog } from 'naive-ui'
import L from 'leaflet'
import { useCitiesStore } from '@/stores/cities'
import { toAdcode, stripCitySuffix } from '@/utils/city'
import { apiError } from '@/api/http'

defineOptions({ name: 'MapView' })

const router = useRouter()
const message = useMessage()
const dialog = useDialog()
const cities = useCitiesStore()

const mapEl = ref<HTMLDivElement | null>(null)
const loading = ref(true)
const loadFailed = ref(false)

let map: L.Map | null = null
// adcode -> 该市的图层，便于点亮状态变化时重绘样式
const layersByAdcode = new Map<string, L.Path[]>()

interface Selected {
  name: string
  adcode: string
  lit: boolean
}
const selected = ref<Selected | null>(null)

// 底图多源 + 自动切换：
// - 高德：国内可达、中文标注、国内细节最好。但授权只完整渲染中国境内，
//   境外低缩放只有灰底轮廓、连地名都没有 —— "外国显示不全"就是它导致的。
// - Esri 世界街图：国内可达（实测 0.85s），全球路网+城市名+国名都完整。
// - Esri 卫星 / OSM（德国镜像，官方源国内超时）：备用与彩蛋。
// 自动规则：缩放 <5（看全球）用 Esri，>=5 且中心在中国用高德；手动在右下角选过就不再自动切。
const GAODE_URL =
  'https://webrd0{s}.is.autonavi.com/appmaptile?lang=zh_cn&size=1&scale=1&style=8&x={x}&y={y}&z={z}'
const ESRI_STREET_URL =
  'https://server.arcgisonline.com/ArcGIS/rest/services/World_Street_Map/MapServer/tile/{z}/{y}/{x}'
const ESRI_SAT_URL =
  'https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}'
const OSM_URL = 'https://tile.openstreetmap.de/{z}/{x}/{y}.png'

type BaseKey = 'gaode' | 'esri' | 'esriSat' | 'osm'
const BASE_NAMES: Record<BaseKey, string> = {
  gaode: '高德中文',
  esri: 'Esri 世界',
  esriSat: 'Esri 卫星',
  osm: 'OSM 国际',
}
// tileerror 兜底链：主源挂了往下一个
const BASE_FALLBACK: Record<BaseKey, BaseKey | null> = {
  gaode: 'esri',
  esri: 'osm',
  esriSat: 'esri',
  osm: null,
}

let baseLayers: Record<BaseKey, L.TileLayer> | null = null
let currentBase: BaseKey = 'esri'
let manualBase: BaseKey | null = null
let autoSwitched = false
// 程序内 switchBase 会触发控件 baselayerchange，用这个标志区分"用户手动选"
let switching = false

// 中国大致范围（自动选底图用，粗略即可）
const CHINA_VIEW = L.latLngBounds(L.latLng(3, 73), L.latLng(54, 136))

function autoBase(): BaseKey {
  if (!map) return 'esri'
  if (map.getZoom() >= 5 && CHINA_VIEW.contains(map.getCenter())) return 'gaode'
  return 'esri'
}

function switchBase(name: BaseKey) {
  if (!map || !baseLayers || name === currentBase) return
  switching = true
  map.removeLayer(baseLayers[currentBase])
  map.addLayer(baseLayers[name])
  currentBase = name
  switching = false
}

// 某一底图连续拉不到瓦片时按链切换，别让用户对着纯色底发呆
function watchTileErrors(layer: L.TileLayer, name: BaseKey) {
  let errs = 0
  layer.on('tileerror', () => {
    if (autoSwitched || manualBase) return
    errs += 1
    const next = BASE_FALLBACK[name]
    if (errs >= 12 && next) {
      autoSwitched = true
      switchBase(next)
      message.info(`${BASE_NAMES[name]}瓦片不可用，已自动切换为${BASE_NAMES[next]}`)
    }
  })
}

function styleFor(adcode: string, isSelected: boolean): L.PathOptions {
  const lit = cities.isLit(adcode)
  if (isSelected) {
    return { color: '#000', weight: 2.5, fillColor: '#000', fillOpacity: lit ? 0.45 : 0.15 }
  }
  if (lit) {
    return { color: 'rgba(0,0,0,0.55)', weight: 0.8, fillColor: '#000', fillOpacity: 0.27 }
  }
  return { color: 'rgba(0,0,0,0.15)', weight: 0.5, fillColor: '#ffffff', fillOpacity: 0.0 }
}

function restyle(adcode: string) {
  const layers = layersByAdcode.get(adcode)
  if (!layers) return
  const isSel = selected.value?.adcode === adcode
  const st = styleFor(adcode, isSel)
  layers.forEach((l) => l.setStyle(st))
}

function restyleAll() {
  for (const adcode of layersByAdcode.keys()) restyle(adcode)
}

function clearSelection() {
  if (selected.value) {
    const prev = selected.value.adcode
    selected.value = null
    restyle(prev)
  }
}

function selectCity(name: string, adcode: string) {
  const prev = selected.value?.adcode
  selected.value = { name, adcode, lit: cities.isLit(adcode) }
  if (prev && prev !== adcode) restyle(prev)
  restyle(adcode)
}

async function onLight() {
  if (!selected.value) return
  const { adcode, name } = selected.value
  try {
    if (selected.value.lit) {
      await cities.unlight(adcode)
      message.success(`已取消点亮 ${name}`)
    } else {
      await cities.light(adcode)
      message.success(`已点亮 ${name}`)
    }
    selected.value = { ...selected.value, lit: !selected.value.lit }
    restyle(adcode)
  } catch (e) {
    message.error(apiError(e, selected.value.lit ? '取消失败' : '点亮失败'))
  }
}

function onUnlightConfirm() {
  if (!selected.value) return
  const { name } = selected.value
  // 文案必须和后端一致：unlight_city 只删「行程 + 点亮记录」，
  // 而且**有账单或笔记时直接拒绝**（要用户先删）—— 以前写"会同时删除账单和笔记"，
  // 用户点确定却收到报错，等于骗了一次。
  dialog.warning({
    title: '取消点亮',
    content: `取消点亮「${name}」会删除该城的行程记录。若该城已有账单或笔记，需要先删除才能取消点亮；已经保存的行程规划会保留。确定继续？`,
    positiveText: '确定取消',
    negativeText: '再想想',
    onPositiveClick: onLight,
  })
}

function goRecords() {
  if (!selected.value) return
  router.push({
    name: 'records',
    query: { adcode: selected.value.adcode, city: selected.value.name },
  })
}

function goPlans() {
  if (!selected.value) return
  router.push({
    name: 'plans',
    query: { adcode: selected.value.adcode, city: selected.value.name },
  })
}

async function initMap() {
  if (!mapEl.value) return
  map = L.map(mapEl.value, {
    center: [35.86, 104.19],
    zoom: 4,
    minZoom: 3,
    // 之前钉在 10，街道级细节根本放不进去；OSM/高德都支持到 18
    maxZoom: 18,
    preferCanvas: true,
    zoomControl: true,
    attributionControl: true,
  })
  const gaode = L.tileLayer(GAODE_URL, {
    subdomains: ['1', '2', '3', '4'],
    maxNativeZoom: 18,
    maxZoom: 19,
    attribution: '&copy; 高德地图',
  })
  const esri = L.tileLayer(ESRI_STREET_URL, {
    maxNativeZoom: 18,
    maxZoom: 19,
    attribution: '&copy; Esri',
  })
  const esriSat = L.tileLayer(ESRI_SAT_URL, {
    maxNativeZoom: 18,
    maxZoom: 19,
    attribution: '&copy; Esri, Maxar',
  })
  // 官方 tile.openstreetmap.org 国内超时，用德国镜像（实测 2.2s 可用）
  const osm = L.tileLayer(OSM_URL, {
    maxNativeZoom: 19,
    maxZoom: 19,
    attribution: '&copy; OpenStreetMap contributors',
  })
  baseLayers = { gaode, esri, esriSat, osm }
  ;(Object.keys(baseLayers) as BaseKey[]).forEach((k) => watchTileErrors(baseLayers![k], k))

  // 初始按视口挑底图（z4 看全球 → Esri；zoom 进中国 → 高德）
  currentBase = autoBase()
  switching = true
  baseLayers[currentBase].addTo(map)
  switching = false

  L.control
    .layers(
      {
        [BASE_NAMES.gaode]: gaode,
        [BASE_NAMES.esri]: esri,
        [BASE_NAMES.esriSat]: esriSat,
        [BASE_NAMES.osm]: osm,
      },
      undefined,
      { position: 'bottomright' },
    )
    .addTo(map)

  // 用户在右下角手动选了底图 → 以后不再自动切
  map.on('baselayerchange', (e) => {
    if (switching) return
    const key = (Object.keys(BASE_NAMES) as BaseKey[]).find(
      (k) => BASE_NAMES[k] === (e as L.LayersControlEvent).name,
    )
    if (key) manualBase = key
  })
  // 移动/缩放结束后按视口自动换底图
  map.on('moveend', () => {
    if (!manualBase) switchBase(autoBase())
  })

  // 容器尺寸一变就重排瓦片（切 tab 回来 / 窗口 resize 都会被它接住）
  ro = new ResizeObserver(() => map?.invalidateSize())
  ro.observe(mapEl.value)

  // 点击空白处清除选中
  map.on('click', clearSelection)

  const resp = await fetch('/china_cities.geojson')
  if (!resp.ok) throw new Error('geojson load failed')
  const data = await resp.json()

  L.geoJSON(data, {
    style: (feature) => {
      const gb = (feature?.properties?.gb as string) || ''
      return styleFor(toAdcode(gb), false)
    },
    onEachFeature: (feature, layer) => {
      const props = feature?.properties || {}
      const gb = (props.gb as string) || ''
      const adcode = toAdcode(gb)
      const name = stripCitySuffix((props.name as string) || '')
      if (!adcode) return

      const path = layer as L.Path
      const arr = layersByAdcode.get(adcode) || []
      arr.push(path)
      layersByAdcode.set(adcode, arr)

      layer.bindTooltip(name, { sticky: true, direction: 'top' })
      layer.on('click', (e) => {
        L.DomEvent.stopPropagation(e)
        selectCity(name, adcode)
      })
    },
  }).addTo(map)
}

async function load() {
  loading.value = true
  loadFailed.value = false
  try {
    await cities.fetchLit()
    await nextTick()
    await initMap()
    restyleAll()
  } catch {
    loadFailed.value = true
  } finally {
    loading.value = false
  }
}

onMounted(load)

// ── 瓦片错位/残缺的修复 ──
// AppShell 用 <keep-alive> 缓存本页：切去别的 tab 再回来（或窗口/侧栏尺寸变化）时，
// 容器尺寸已经变了，但 Leaflet 不会自己发现 —— 瓦片只按旧尺寸排布，
// 实测表现为"地图中间一块正常、四周大片空白/灰底"。必须手动 invalidateSize。
// ResizeObserver 能同时覆盖：切 tab、窗口 resize、侧栏动画这几种来源。
let ro: ResizeObserver | null = null

onActivated(() => {
  // keep-alive 重新激活：此时 DOM 尺寸可能刚恢复，等一帧再刷
  nextTick(() => map?.invalidateSize())
  // 点亮状态可能在别的页面变了（比如聊天里点亮了城市，后端直接写库），
  // 本页被 keep-alive 缓存时 store 里还是旧的 → 切回来必须重拉一次再重绘
  if (map) {
    cities
      .fetchLit()
      .then(() => restyleAll())
      .catch(() => {})
  }
})

onBeforeUnmount(() => {
  ro?.disconnect()
  ro = null
  if (map) {
    map.remove()
    map = null
  }
  baseLayers = null
  layersByAdcode.clear()
})
</script>

<template>
  <div class="map-view">
    <div ref="mapEl" class="map-canvas"></div>

    <div v-if="loading" class="overlay">
      <div class="overlay-box ft-card">加载地图中…</div>
    </div>

    <div v-else-if="loadFailed" class="overlay">
      <div class="overlay-box ft-card fail">
        <div class="fail-icon">
          <svg viewBox="0 0 24 24" width="40" height="40" aria-hidden="true">
            <path
              d="M3 9c5-4.5 13-4.5 18 0M6.2 12.6c3.4-2.9 8.2-2.9 11.6 0M9.4 16.1c1.7-1.4 3.9-1.4 5.2 0"
              fill="none"
              stroke="var(--ft-text-3)"
              stroke-width="1.4"
              stroke-linecap="round"
            />
            <path d="M4 4l16 16" stroke="var(--ft-text-3)" stroke-width="1.4" stroke-linecap="round" />
            <circle cx="12" cy="19.6" r="1.3" fill="var(--ft-accent)" />
          </svg>
        </div>
        <div class="fail-title">地图加载失败</div>
        <div class="fail-text">请检查网络或后端服务后重试</div>
        <button class="ft-btn" @click="load">重新加载</button>
      </div>
    </div>

    <!-- 选中城市的操作卡片 -->
    <transition name="pop">
      <div v-if="selected" class="city-card ft-card">
        <div class="city-head">
          <span class="city-name">{{ selected.name }}</span>
          <span class="ft-tag" :class="{ 'tag-lit': selected.lit }">
            {{ selected.lit ? '已点亮' : '未点亮' }}
          </span>
          <button class="close" @click="clearSelection">✕</button>
        </div>
        <div class="city-adcode">adcode · {{ selected.adcode }}</div>
        <div class="city-actions">
          <button v-if="!selected.lit" class="ft-btn" @click="onLight">点亮这座城</button>
          <button v-else class="ft-btn ft-btn--ghost" @click="onUnlightConfirm">取消点亮</button>
          <button class="ft-btn ft-btn--ghost" :disabled="!selected.lit" @click="goRecords">
            记录
          </button>
          <button class="ft-btn ft-btn--ghost" @click="goPlans">规划</button>
        </div>
      </div>
    </transition>

    <div class="legend ft-card">
      <div class="legend-row"><span class="swatch lit"></span> 已点亮</div>
      <div class="legend-row"><span class="swatch"></span> 未点亮</div>
      <div class="legend-tip">看全球自动用 Esri、放大中国自动用高德 · 右下角可手动换底图</div>
    </div>
  </div>
</template>

<style scoped>
.map-view {
  position: relative;
  width: 100%;
  height: 100%;
}
.map-canvas {
  position: absolute;
  inset: 0;
  background: #eae6df;
}
/* 底图做轻度去色，贴合本页黑白报纸风，点亮的城市块因此更突出 */
.map-canvas :deep(.leaflet-tile) {
  filter: grayscale(0.85) contrast(0.92);
}

.overlay {
  position: absolute;
  inset: 0;
  display: grid;
  place-items: center;
  background: rgba(251, 249, 249, 0.7);
  z-index: 500;
}
.overlay-box {
  padding: 18px 24px;
  font-size: 14px;
  font-weight: 600;
  text-align: center;
}
.overlay-box.fail {
  display: flex;
  flex-direction: column;
  gap: 10px;
  align-items: center;
}
.fail-icon {
  font-size: 32px;
}
.fail-title {
  font-size: 16px;
  font-weight: 700;
}
.fail-text {
  font-size: 13px;
  color: var(--c-grey-text);
}

.city-card {
  position: absolute;
  top: 16px;
  right: 16px;
  z-index: 600;
  width: 280px;
  padding: 14px;
}
.city-head {
  display: flex;
  align-items: center;
  gap: 8px;
}
.city-name {
  font-size: 17px;
  font-weight: 800;
}
.tag-lit {
  color: var(--c-black);
  background: #ffe45e;
  border: 1.5px solid var(--c-black);
}
.close {
  margin-left: auto;
  width: 24px;
  height: 24px;
  font-size: 13px;
  color: var(--c-black);
  background: var(--c-white);
  border: 1.5px solid var(--c-black);
  border-radius: 6px;
}
.city-adcode {
  margin: 8px 0 12px;
  font-size: 12px;
  color: var(--c-grey-text);
}
.city-actions {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 8px;
}
.city-actions .ft-btn:first-child {
  grid-column: 1 / -1;
}

.legend {
  position: absolute;
  bottom: 16px;
  left: 16px;
  z-index: 600;
  padding: 10px 12px;
  font-size: 12px;
}
.legend-row {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 4px;
}
.swatch {
  width: 16px;
  height: 12px;
  background: #fff;
  border: 1.5px solid rgba(0, 0, 0, 0.4);
  border-radius: 3px;
}
.swatch.lit {
  background: rgba(0, 0, 0, 0.27);
  border-color: #000;
}
.legend-tip {
  margin-top: 6px;
  color: var(--c-grey-text);
}

.pop-enter-active,
.pop-leave-active {
  transition: opacity 0.12s ease, transform 0.12s ease;
}
.pop-enter-from,
.pop-leave-to {
  opacity: 0;
  transform: translateY(-6px);
}
</style>
