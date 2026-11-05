// 把 web/src 的內容雜湊寫進 static/.build-stamp，
// 讓 tools/check_frontend.sh 能發現「改了原始碼忘記 build」。
import { createHash } from 'node:crypto'
import { readdirSync, readFileSync, statSync, writeFileSync } from 'node:fs'
import { join, dirname, extname } from 'node:path'
import { fileURLToPath } from 'node:url'

const here = dirname(fileURLToPath(import.meta.url))
const srcDir = join(here, 'src')
const out = join(here, '..', 'src', 'vidgrab', 'static', '.build-stamp')

function walk(dir) {
  const out = []
  for (const name of readdirSync(dir).sort()) {
    const p = join(dir, name)
    if (statSync(p).isDirectory()) out.push(...walk(p))
    else if (['.ts', '.tsx', '.css'].includes(extname(p))) out.push(p)
  }
  return out
}

const hash = createHash('sha256')
for (const f of walk(srcDir)) {
  hash.update(readFileSync(f))
}
const digest = hash.digest('hex')
writeFileSync(out, digest + '\n')
console.log('stamped', digest.slice(0, 12))
