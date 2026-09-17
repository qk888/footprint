// 城市相关工具：adcode 解析、名称处理
// GeoJSON 里每个市的 properties.gb 形如 "156441400"，后 6 位即 adcode

/** 从 gb 编码取后 6 位作为 adcode */
export function toAdcode(gbCode: string): string {
  return gbCode && gbCode.length >= 6 ? gbCode.slice(-6) : gbCode
}

/** 城市名去掉「市」后缀 */
export function stripCitySuffix(name: string): string {
  return (name || '').replace(/市$/, '')
}
