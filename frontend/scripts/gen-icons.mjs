// Generate raster app icons from the source SVG.
//   src/renderer/src/public/icons/app-icon.svg  ->  build/icon.ico, build/icon.png, build/icon@256.png
// Run: npm run icons
import { Resvg } from '@resvg/resvg-js'
import pngToIco from 'png-to-ico'
import { mkdir, readFile, writeFile } from 'node:fs/promises'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const SVG = join(root, 'src/renderer/src/public/icons/app-icon.svg')
const OUT = join(root, 'build')

// Windows .ico carries multiple resolutions; the OS picks the right one per context
// (16/24/32/48 = title bar + taskbar, 256 = large/Explorer).
const ICO_SIZES = [16, 24, 32, 48, 64, 128, 256]

const renderPng = (svg, size) =>
  Buffer.from(
    new Resvg(svg, { fitTo: { mode: 'width', value: size }, background: 'rgba(0,0,0,0)' })
      .render()
      .asPng()
  )

const main = async () => {
  const svg = await readFile(SVG, 'utf8')
  await mkdir(OUT, { recursive: true })

  const pngs = ICO_SIZES.map((s) => renderPng(svg, s))
  await writeFile(join(OUT, 'icon.ico'), await pngToIco(pngs))
  await writeFile(join(OUT, 'icon.png'), renderPng(svg, 512)) // Linux / generic
  await writeFile(join(OUT, 'icon@256.png'), renderPng(svg, 256))

  console.log(`✓ icons written to ${OUT} (ico: ${ICO_SIZES.join('/')}, png: 512, 256)`)
}

main().catch((e) => {
  console.error('icon generation failed:', e)
  process.exit(1)
})
