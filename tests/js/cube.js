/* Does the index find the model's edges, or the tessellation's?

   A cube subdivided 8x8 per face is 768 triangles with well over a thousand
   interior edges, and exactly 12 edges a person would call an edge. Twelve
   means the index is reading the model. Hundreds means it is reading the
   mesh, which is the failure this exists to catch. */

function subdividedCube(size, n) {
  const h = size / 2
  const tris = []
  const face = (origin, u, v, flip) => {
    for (let i = 0; i < n; i++) {
      for (let j = 0; j < n; j++) {
        const p = (a, b) => [
          origin[0] + u[0] * (a / n) + v[0] * (b / n),
          origin[1] + u[1] * (a / n) + v[1] * (b / n),
          origin[2] + u[2] * (a / n) + v[2] * (b / n),
        ]
        const q = [p(i, j), p(i + 1, j), p(i + 1, j + 1), p(i, j + 1)]
        const t = flip
          ? [[q[0], q[2], q[1]], [q[0], q[3], q[2]]]
          : [[q[0], q[1], q[2]], [q[0], q[2], q[3]]]
        for (const tri of t) for (const point of tri) tris.push(point[0], point[1], point[2])
      }
    }
  }
  const s = size
  face([-h, -h, h], [s, 0, 0], [0, s, 0], false)
  face([-h, -h, -h], [s, 0, 0], [0, s, 0], true)
  face([-h, h, -h], [s, 0, 0], [0, 0, s], false)
  face([-h, -h, -h], [s, 0, 0], [0, 0, s], true)
  face([h, -h, -h], [0, s, 0], [0, 0, s], false)
  face([-h, -h, -h], [0, s, 0], [0, 0, s], true)
  return new Float32Array(tris)
}

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

let failures = 0
const check = (label, got, want) => {
  const ok = Math.abs(got - want) < 1e-6 || got === want
  if (!ok) failures++
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${label}: ${got}${ok ? '' : ' (expected ' + want + ')'}`)
}

const positions = subdividedCube(100, 8)
console.log(`cube: ${positions.length / 9} triangles, ${positions.length / 3} raw vertices`)

buildSnapIndex(geometryFrom(positions))
console.log(
  `      ${snap.verts.length / 3} merged vertices, ${snap.count} feature edge segments, ` +
  `${snap.cornerCount} corners`,
)
// Each of the twelve edges arrives cut into eight segments, so 96 is the
// right answer; what matters is that the 1,056 interior edges are gone.
check('feature edge segments on a subdivided cube', snap.count, 96)
check('corners on a cube', snap.cornerCount, 8)

// A tolerance of 14 world units, so the numbers below are millimetres.
pixelsPerUnit = () => 1
snap.on = true

const at = (x, y, z) => new Vector3(x, y, z)

const corner = snapTo(at(48, 48, 51))
check('a click near a corner snaps to a vertex', corner.kind, 'vertex')
check('  corner x', corner.point.x, 50)
check('  corner y', corner.point.y, 50)
check('  corner z', corner.point.z, 50)

const edge = snapTo(at(0, 47, 52))
check('a click near an edge snaps to the edge', edge.kind, 'edge')
check('  edge y', edge.point.y, 50)
check('  edge z', edge.point.z, 50)
check('  edge keeps its position along the edge', edge.point.x, 0)

const face = snapTo(at(0, 0, 50))
check('the middle of a face does not snap', face.kind, 'surface')

snap.on = false
check('snapping off gives the surface point back', snapTo(at(48, 48, 51)).kind, 'surface')
snap.on = true

// The span a cube gives up has to be the cube: corner to corner across one
// face is 100 mm exactly, and that only holds if both ends snapped.
const a = snapTo(at(47, 47, 51)).point
const b = snapTo(at(-47, 47, 51)).point
check('corner to corner across a face', a.distanceTo(b), 100)

globalThis.__failures = failures
