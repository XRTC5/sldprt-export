/* The smallest THREE the snapping module actually uses, plus stubs for the
   browser-only names it closes over. */

class Vector3 {
  constructor(x = 0, y = 0, z = 0) { this.x = x; this.y = y; this.z = z }
  set(x, y, z) { this.x = x; this.y = y; this.z = z; return this }
  copy(v) { return this.set(v.x, v.y, v.z) }
  clone() { return new Vector3(this.x, this.y, this.z) }
  sub(v) { this.x -= v.x; this.y -= v.y; this.z -= v.z; return this }
  subVectors(a, b) { return this.set(a.x - b.x, a.y - b.y, a.z - b.z) }
  addScaledVector(v, s) { this.x += v.x * s; this.y += v.y * s; this.z += v.z * s; return this }
  dot(v) { return this.x * v.x + this.y * v.y + this.z * v.z }
  lengthSq() { return this.dot(this) }
  distanceTo(v) { return Math.hypot(this.x - v.x, this.y - v.y, this.z - v.z) }
}

class Box3 {
  constructor() {
    this.min = new Vector3(Infinity, Infinity, Infinity)
    this.max = new Vector3(-Infinity, -Infinity, -Infinity)
  }
  getSize(t) {
    return t.set(this.max.x - this.min.x, this.max.y - this.min.y, this.max.z - this.min.z)
  }
}

const THREE = { Vector3 }

let renderer = null
let camera = null
let solid = null
const showHint = () => {}
const num = (n) => String(n)
