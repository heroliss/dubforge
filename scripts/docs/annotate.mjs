// 在原始截图上合成可复现的 SVG 标注，保留底图及坐标记录。
import fs from 'node:fs/promises'
import path from 'node:path'
import crypto from 'node:crypto'
import sharp from 'sharp'

const args = process.argv.slice(2)
if (args.length !== 3 || args.includes('--help')) {
  console.log('用法：node scripts/docs/annotate.mjs 原图.png 标注.json 输出.webp')
  process.exit(args.includes('--help') ? 0 : 2)
}
const [input, specFile, output] = args.map(value => path.resolve(value))
if (path.extname(output).toLowerCase() !== '.webp') throw new Error('输出必须是独立的 .webp 文件')
const receiptFile = output + '.json'
const imageTemporary = output + '.' + crypto.randomUUID() + '.tmp'
const receiptTemporary = receiptFile + '.' + crypto.randomUUID() + '.tmp'
const allPaths = [input, specFile, output, receiptFile, imageTemporary, receiptTemporary]
const foldPath = value => process.platform === 'win32' ? value.toLowerCase() : value

async function fileIdentity(file) {
  // 输出尚不存在时，仍解析最近的现存父目录，覆盖 junction/symlink 的别名。
  let current = file
  const missing = []
  while (true) {
    try {
      current = await fs.realpath(current)
      break
    } catch (error) {
      if (error.code !== 'ENOENT') throw error
      const parent = path.dirname(current)
      if (parent === current) throw error
      missing.unshift(path.basename(current))
      current = parent
    }
  }
  const canonical = foldPath(path.normalize(path.join(current, ...missing)))
  let stat = null
  try {
    stat = await fs.stat(file, { bigint: true })
    if (!stat.isFile()) throw new Error('输入、标注及输出路径必须指向普通文件')
  } catch (error) {
    if (error.code !== 'ENOENT') throw error
  }
  return { canonical, stat }
}

function sameInode(a, b) {
  return a && b && a.ino !== 0n && b.ino !== 0n && a.dev === b.dev && a.ino === b.ino
}

async function assertDistinctFiles() {
  const identities = await Promise.all(allPaths.map(fileIdentity))
  for (let a = 0; a < identities.length; a++) {
    for (let b = a + 1; b < identities.length; b++) {
      if (identities[a].canonical === identities[b].canonical || sameInode(identities[a].stat, identities[b].stat)) {
        throw new Error('原图、标注清单、图片、收据及临时文件必须是不同文件，拒绝覆盖或路径别名')
      }
    }
  }
}

await assertDistinctFiles()
const bytes = await fs.readFile(input)
const meta = await sharp(bytes).metadata()
const { width: originalWidth, height: originalHeight } = meta
if (!originalWidth || !originalHeight) throw new Error('无法读取截图尺寸')
const spec = JSON.parse(await fs.readFile(specFile, 'utf8'))
if (!Array.isArray(spec.marks) || !spec.marks.length) throw new Error('标注清单至少需要一个目标')
// 裁剪坐标以原图为基准，标注坐标以裁剪后的画布为基准。
const crop = spec.crop === undefined
  ? { left: 0, top: 0, width: originalWidth, height: originalHeight }
  : spec.crop
if (!crop || typeof crop !== 'object' || Array.isArray(crop)
    || Object.keys(crop).some(key => !['left', 'top', 'width', 'height'].includes(key))
    || !['left', 'top', 'width', 'height'].every(key => Number.isInteger(crop[key]))
    || crop.left < 0 || crop.top < 0 || crop.width <= 0 || crop.height <= 0
    || crop.left + crop.width > originalWidth || crop.top + crop.height > originalHeight) {
  throw new Error('裁剪范围必须是原图内的整数矩形 {left,top,width,height}')
}
const { width, height } = crop
const maxWidth = spec.maxWidth ?? 1180
if (!Number.isInteger(maxWidth) || maxWidth < 640 || maxWidth > 1200) throw new Error('输出宽度应在 640 至 1200 像素之间')
const number = value => {
  if (typeof value !== 'number' || !Number.isFinite(value)) throw new Error('标注坐标必须是有限数值')
  return value
}
const pieces = []
for (const [index, mark] of spec.marks.entries()) {
  const target = mark.target || {}
  const x = number(target.x), y = number(target.y), w = number(target.width), h = number(target.height)
  if (x < 0 || y < 0 || w <= 0 || h <= 0 || x + w > width + 1 || y + h > height + 1) {
    throw new Error(`第 ${index + 1} 个目标超出原图或裁剪画布，检查裁剪原点与设备像素比`)
  }
  const cx = mark.label ? number(mark.label.x) : Math.max(23, x - 23)
  const cy = mark.label ? number(mark.label.y) : Math.max(23, y - 23)
  if (cx < 21 || cy < 21 || cx > width - 21 || cy > height - 21) throw new Error('编号位置超出原图或裁剪画布')
  const id = mark.number ?? index + 1
  if (!Number.isInteger(id) || id < 1 || id > 99) throw new Error('编号必须是 1 至 99 的整数')
  const outline = mark.shape === 'ellipse'
    ? `<ellipse cx="${x + w / 2}" cy="${y + h / 2}" rx="${w / 2}" ry="${h / 2}"/>`
    : `<rect x="${x}" y="${y}" width="${w}" height="${h}" rx="8"/>`
  pieces.push(`<g fill="none" stroke="#fff" stroke-width="6" opacity=".9">${outline}</g>`)
  pieces.push(`<g fill="none" stroke="#f0a35b" stroke-width="3">${outline}</g>`)
  if (mark.arrow !== false) {
    const tx = x + Math.min(16, w / 2), ty = y + Math.min(8, h / 2)
    const dx = tx - cx, dy = ty - cy, length = Math.hypot(dx, dy)
    if (length > 37) {
      const sx = cx + dx / length * 22, sy = cy + dy / length * 22
      pieces.push(`<path d="M${sx},${sy} L${tx},${ty}" fill="none" stroke="#fff" stroke-width="7"/>`)
      pieces.push(`<path d="M${sx},${sy} L${tx},${ty}" fill="none" stroke="#f0a35b" stroke-width="3" marker-end="url(#arrow)"/>`)
    }
  }
  pieces.push(`<circle cx="${cx}" cy="${cy}" r="19" fill="#f0a35b" stroke="#fff" stroke-width="3"/>`)
  pieces.push(`<text x="${cx}" y="${cy + 6}" text-anchor="middle" font-family="sans-serif" font-weight="700" font-size="18" fill="#17212b">${id}</text>`)
}
const svg = Buffer.from(`<svg xmlns="http://www.w3.org/2000/svg" width="${width}" height="${height}"><defs><marker id="arrow" markerWidth="8" markerHeight="8" refX="7" refY="4" orient="auto"><path d="M0,0 L8,4 L0,8 Z" fill="#f0a35b"/></marker></defs>${pieces.join('')}</svg>`)
// 先裁剪，再合成，最后缩放；分开管线，防止操作排序改变标注坐标。
const canvas = spec.crop === undefined ? bytes : await sharp(bytes).extract(crop).png().toBuffer()
const composite = await sharp(canvas).composite([{ input: svg }]).png().toBuffer()
const rendered = await sharp(composite).resize({ width: maxWidth, withoutEnlargement: true }).webp({ lossless: true }).toBuffer()
const digest = value => crypto.createHash('sha256').update(value).digest('hex')
const receipt = { input: path.basename(input), input_sha256: digest(bytes), output_sha256: digest(rendered),
  original_size: { width: originalWidth, height: originalHeight }, crop, spec }
await fs.mkdir(path.dirname(output), { recursive: true })
await assertDistinctFiles()
const ownedTemporaries = []
async function writeTemporary(file, content) {
  // 独占创建，不追随或覆盖既有临时文件；两种产物都先落临时名。
  const handle = await fs.open(file, 'wx', 0o600)
  try {
    ownedTemporaries.push({ file, stat: await handle.stat({ bigint: true }) })
    await handle.writeFile(content)
    await handle.sync()
  } finally {
    await handle.close()
  }
}
try {
  await writeTemporary(imageTemporary, rendered)
  await writeTemporary(receiptTemporary, JSON.stringify(receipt, null, 2) + '\n')
  await assertDistinctFiles()
  // 每个文件独立原子替换；图片和收据不是跨文件事务。
  await fs.rename(imageTemporary, output)
  await fs.rename(receiptTemporary, receiptFile)
} finally {
  for (const owned of ownedTemporaries) {
    try {
      const current = await fs.lstat(owned.file, { bigint: true })
      if (sameInode(current, owned.stat)) await fs.unlink(owned.file)
    } catch (error) {
      if (error.code !== 'ENOENT') throw error
    }
  }
}
console.log(JSON.stringify({ output, bytes: rendered.length, marks: spec.marks.length }))
