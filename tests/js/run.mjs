/* Tests for the viewer's snapping index.

   The module under test lives inside `app/static/index.html`, because the
   viewer is one file that a browser opens directly. Rather than keep a second
   copy in step, this cuts the module out of the page at the same two comment
   markers the page uses to delimit it, so what runs here is what ships.

   Run with:  node tests/js/run.mjs
   Optionally against real CAD, once tests/js/fixtures/<name>.f32 exists
   (written by tests/js/dump_fixture.py):  node tests/js/run.mjs macaw
*/

import { readFileSync, existsSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'
import vm from 'node:vm'

const here = dirname(fileURLToPath(import.meta.url))
const root = join(here, '..', '..')

const START = '// -------------------------------------------------------------------- snap'
const END = '// ------------------------------------------------------------------ measure'

function snapModule() {
  const page = readFileSync(join(root, 'app', 'static', 'index.html'), 'utf8')
  const a = page.indexOf(START)
  const b = page.indexOf(END)
  if (a < 0 || b < 0 || b < a) {
    throw new Error('the snapping module is no longer delimited in index.html')
  }
  return page.slice(a, b)
}

const read = (name) => readFileSync(join(here, name), 'utf8')

const fixture = process.argv[2]
let positions = null
if (fixture) {
  const path = join(here, 'fixtures', `${fixture}.f32`)
  if (!existsSync(path)) {
    console.error(`no fixture at ${path} — run tests/js/dump_fixture.py first`)
    process.exit(2)
  }
  const raw = readFileSync(path)
  positions = new Float32Array(raw.buffer, raw.byteOffset, raw.length / 4)
}

const context = vm.createContext({
  console,
  globalThis: null,
  __positions: positions,
  __label: fixture ?? 'cube',
})
context.globalThis = context

const driver = fixture ? 'real.js' : 'cube.js'
vm.runInContext([read('shim.js'), snapModule(), read(driver)].join('\n'), context, {
  filename: 'snap-under-test.js',
})

process.exit(context.__failures ? 1 : 0)
