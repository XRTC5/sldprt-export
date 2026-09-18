/* The same index against real CAD: the MACAW assembly, 488,914 triangles,
   which is the largest thing the app currently opens. What matters here is
   that the build finishes in a reasonable time, that it throws most of the
   mesh away (a few per cent of edges should survive as features), and that a
   click resolves without walking the whole model. */

function geometryFrom(positions) {
  const box = new Box3()
  for (let i = 0; i < positions.length; i += 3) {
    box.min.set(
      Math.min(box.min.x, positions[i]),
      Math.min(box.min.y, positions[i + 1]),
      Math.min(box.min.z, positions[i + 2]),
    )
    box.max.set(
      Math.max(box.max.x, positions[i]),
      Math.max(box.max.y, positions[i + 1]),
      Math.max(box.max.z, positions[i + 2]),
    )
  }
  return {
    attributes: { position: { array: positions } },
    boundingBox: box,
    computeBoundingBox() {},
  }
}

const positions = globalThis.__positions
const triangles = positions.length / 9
console.log(`${globalThis.__label}: ${triangles.toLocaleString()} triangles`)

const t0 = Date.now()
const ok = buildSnapIndex(geometryFrom(positions))
const build = Date.now() - t0

const merged = snap.verts.length / 3
console.log(`  built in ${build} ms (${ok ? 'ok' : 'REFUSED'})`)
console.log(`  ${merged.toLocaleString()} merged vertices of ${(positions.length / 3).toLocaleString()}`)
console.log(`  ${snap.count.toLocaleString()} feature edge segments of ~${Math.round(triangles * 1.5).toLocaleString()} mesh edges`)
console.log(`  ${snap.cornerCount.toLocaleString()} corners`)
console.log(`  ${snap.cells.size.toLocaleString()} occupied grid cells`)

// A click anywhere on the model: the query has to be quick and it has to be
// looking at a small neighbourhood, not the whole index.
pixelsPerUnit = () => 4
snap.on = true

let hits = 0
const t1 = Date.now()
for (let i = 0; i < 2000; i++) {
  const v = (i * 7919) % (positions.length / 3)
  const p = new Vector3(positions[v * 3], positions[v * 3 + 1], positions[v * 3 + 2])
  if (snapTo(p).kind !== 'surface') hits++
}
const query = Date.now() - t1
console.log(`  2,000 clicks in ${query} ms (${(query / 2000).toFixed(3)} ms each), ${hits} snapped`)

let failures = 0
const check = (label, pass, detail) => {
  if (!pass) failures++
  console.log(`${pass ? 'PASS' : 'FAIL'}  ${label}${detail ? ': ' + detail : ''}`)
}

check('index builds', ok)
check('the tessellation is discarded', snap.count < triangles * 0.3,
  `${((snap.count / (triangles * 1.5)) * 100).toFixed(1)}% of mesh edges kept`)
check('corners are rarer than edges', snap.cornerCount < snap.count)
// Five seconds once, behind a hint, on the largest thing the tool opens.
check('the build stays inside five seconds', build < 5000, `${build} ms`)
// A click already costs a raycast over every triangle, so a few milliseconds
// on top is not what anyone will notice.
check('a click stays inside three milliseconds', query / 2000 < 3, `${(query / 2000).toFixed(3)} ms`)

// Every vertex of the model is on the model, so a click sitting exactly on one
// should almost always find something. Curved surfaces have no creases, so
// this is a majority, not a certainty.
check('clicks on the model mostly snap', hits > 1000, `${hits} of 2000`)

globalThis.__failures = failures
