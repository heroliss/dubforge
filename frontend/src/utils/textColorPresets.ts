export type TextColorPair = { color: string; outline_color: string }
export const TEXT_COLOR_GROUPS = [
  { id: 'warm', name: '活力暖色' }, { id: 'fresh', name: '清新梦幻' },
  { id: 'ink', name: '浅景深字' }, { id: 'soft', name: '柔和中性' },
] as const
export type TextColorPreset = TextColorPair & { id: string; name: string; group: typeof TEXT_COLOR_GROUPS[number]['id']; scene: string; rationale: string }

// 人工策划色相/场景，再按 sRGB 亮度对比与 OKLab 色差校验；不是随机生成或自动认定美观。
// 配色只修改两个色值，不绑定字号、描边宽度、底色、语言或时间。
export const TEXT_COLOR_PRESETS: readonly TextColorPreset[] = [
  { id: 'sunshine', group: 'warm', name: '晴日金', color: '#FFD65A', outline_color: '#30213A', scene: '美食 · 旅行 · 活力',
    rationale: '高明度金黄配低明度紫灰，以冷暖反差增加活力，描边降低彩度避免争抢主体。' },
  { id: 'orange', group: 'warm', name: '橘子汽水', color: '#FFA629', outline_color: '#2F2015', scene: '街头 · 运动 · 元气',
    rationale: '比浅珊瑚更浓的橘金配深焦糖，强化暖色动感，同时保留明显的亮度差。' },
  { id: 'coral', group: 'warm', name: '珊瑚橙', color: '#FFAE8F', outline_color: '#3B2030', scene: '生活 · 人物 · 温暖',
    rationale: '浅珊瑚橙配深红褐，同属暖色但拉开亮度，保留亲近感，避免深红字在暗处难读。' },
  { id: 'rose', group: 'warm', name: '莓果粉', color: '#FFA8CD', outline_color: '#321B38', scene: '综艺 · 萌宠 · 轻快',
    rationale: '明亮玫粉配低明度梅紫，用相近色相维持统一，以明度差而非红绿撞色保证轮廓。' },
  { id: 'berry', group: 'warm', name: '樱桃气泡', color: '#FF80A4', outline_color: '#251025', scene: '庆祝 · 演出 · 热烈',
    rationale: '更高彩度的樱桃粉配极深酒紫，用亮粉替代低亮度纯红，鲜明但仍保持亮度对比。' },
  { id: 'olive', group: 'fresh', name: '青柠芽', color: '#BCE86A', outline_color: '#26351A', scene: '园艺 · 露营 · 春日',
    rationale: '黄绿色文字配深橄榄绿，与薄荷的蓝绿色方向区分，适合更有生长感的画面。' },
  { id: 'mint', group: 'fresh', name: '薄荷绿', color: '#8FF0C5', outline_color: '#123D35', scene: '自然 · 户外 · 清新',
    rationale: '浅薄荷配深墨绿，同色系明暗搭配，有色彩感但不使用刺眼的荧光绿。' },
  { id: 'sky', group: 'fresh', name: '海风蓝', color: '#8DE6FF', outline_color: '#132D4B', scene: '海边 · 科技 · 清爽',
    rationale: '高明度青蓝配深海军蓝，避免深蓝文字亮度过低，形成清晰的冷色轮廓。' },
  { id: 'periwinkle', group: 'fresh', name: '矢车菊', color: '#8AACFF', outline_color: '#152139', scene: '数码 · 城市 · 理性',
    rationale: '偏蓝的中高彩度文字配深蓝灰，区别于偏青的海风蓝和偏紫的鸢尾，用明暗分离稳住轮廓。' },
  { id: 'lavender', group: 'fresh', name: '鸢尾紫', color: '#D1BAFF', outline_color: '#281C46', scene: '音乐 · 夜景 · 梦幻',
    rationale: '浅紫与深靛紫用明度分层，柔化紫色的厚重感，保留偏梦幻的冷色气质。' },
  { id: 'ivory', group: 'soft', name: '暖象牙', color: '#F7F3E8', outline_color: '#202631', scene: '纪实 · 访谈 · 耐看',
    rationale: '保留原来的暖象牙白与深蓝灰，低彩度、明暗分离，适合需要克制的长时间阅读。' },
  { id: 'ink', group: 'ink', name: '纸墨蓝', color: '#233247', outline_color: '#FFF1CF', scene: '书画 · 浅景 · 复古',
    rationale: '反转为深蓝灰文字与浅奶油描边，适合纸张、雪景等明亮画面；须保留足够描边。' },
  { id: 'forest', group: 'ink', name: '森林墨', color: '#1F5549', outline_color: '#E6FFEA', scene: '茶饮 · 植物 · 浅景',
    rationale: '深绿文字搭配极浅绿描边，反转薄荷式的明暗关系，在浅景中保留安静的自然气质。' },
  { id: 'wine', group: 'ink', name: '葡萄酒', color: '#702F4B', outline_color: '#FFE3DB', scene: '人文 · 穿搭 · 典雅',
    rationale: '酒红文字配浅暖粉描边，以浅轮廓支撑深字；与亮粉方案形成不同的明度重心。' },
  { id: 'cocoa', group: 'ink', name: '可可纸', color: '#69432D', outline_color: '#FFF0C5', scene: '咖啡 · 烘焙 · 怀旧',
    rationale: '可可棕与浅奶油同属暖色，深字浅描边，区别于活泼橙字，偏沉稳的复古气质。' },
  { id: 'royal', group: 'ink', name: '靛蓝瓷', color: '#303E80', outline_color: '#EDF4FF', scene: '建筑 · 展览 · 浅景',
    rationale: '靛蓝文字配近白冷蓝轮廓，较纸墨蓝提高彩度，在明亮建筑或展示画面中更有色彩识别度。' },
]

export function normalizeTextColor(value: unknown): string | undefined {
  return typeof value === 'string' && /^#[0-9a-f]{6}$/i.test(value) ? value.toUpperCase() : undefined
}

export function matchingTextColorPreset(color: unknown, outlineColor: unknown) {
  return TEXT_COLOR_PRESETS.find(preset => preset.color === normalizeTextColor(color)
    && preset.outline_color === normalizeTextColor(outlineColor))
}

export function textColorPatch(id: string, prefix = ''): Record<string, string> | null {
  const preset = TEXT_COLOR_PRESETS.find(item => item.id === id)
  return preset ? { [`${prefix}color`]: preset.color, [`${prefix}outline_color`]: preset.outline_color } : null
}

// W3C 相对亮度公式，仅评价色对；薄描边、抗锯齿与运动背景须在实际画面中检查。
export function colorPairContrast(color: unknown, outlineColor: unknown): number | null {
  const colors = [normalizeTextColor(color), normalizeTextColor(outlineColor)]
  if (colors.some(value => !value)) return null
  const luminance = (hex: string) => {
    const [r, g, b] = [1, 3, 5].map(index => {
      const channel = parseInt(hex.slice(index, index + 2), 16) / 255
      return channel <= .04045 ? channel / 12.92 : ((channel + .055) / 1.055) ** 2.4
    })
    return .2126 * r + .7152 * g + .0722 * b
  }
  const [a, b] = colors.map(value => luminance(value!))
  return (Math.max(a, b) + .05) / (Math.min(a, b) + .05)
}
