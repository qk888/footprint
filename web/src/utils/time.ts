// 后端所有时间戳都来自数据库(MySQL 容器跑在 UTC), 且序列化时**不带时区后缀**。
// 直接 new Date("2026-09-14T08:00:10") 会按本地时间解析 → 显示比真实时间少 8 小时。
// 这里统一把"不带时区的时间串"当 UTC 解析, 再按浏览器本地时区展示。
function toDate(s?: string | null): Date | null {
  if (!s) return null
  const iso = s.includes('T') ? s : s.replace(' ', 'T')
  const hasTz = /[zZ]$/.test(iso) || /[+-]\d{2}:?\d{2}$/.test(iso)
  const d = new Date(hasTz ? iso : `${iso}Z`)
  return Number.isNaN(d.getTime()) ? null : d
}

/** 完整时间: 2026/9/14 16:00:10 */
export function fmtDateTime(s?: string | null): string {
  const d = toDate(s)
  return d ? d.toLocaleString('zh-CN', { hour12: false }) : s || ''
}

/** 紧凑时间(精确到秒): 09-14 16:00:10 */
export function fmtTime(s?: string | null): string {
  const d = toDate(s)
  if (!d) return ''
  const p = (n: number) => String(n).padStart(2, '0')
  return `${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`
}
