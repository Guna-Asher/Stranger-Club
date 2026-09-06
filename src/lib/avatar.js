// Deterministic pixel/identicon pattern generator — a pure function of an
// integer design ID (see backend PlayerProfile.avatar_design_id). The same
// ID always produces the exact same pattern and color, computed fresh every
// render rather than stored anywhere: nothing about a design's appearance
// lives server-side. Palette stays within the existing Stranger Club design
// system (lime plus the muted tones already used for status badges).
const PALETTE = ['#d4ff36', '#145aa3', '#805600', '#3a5900', '#a92419', '#58615a'];

function mulberry32(seed) {
  let a = seed >>> 0;
  return function next() {
    a = (a + 0x6d2b79f5) | 0;
    let t = Math.imul(a ^ (a >>> 15), 1 | a);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

export function avatarPattern(designId) {
  const rand = mulberry32(Math.imul(designId, 2654435761));
  const color = PALETTE[Math.floor(rand() * PALETTE.length)];
  // A 5-wide grid, only the left 3 columns are randomized and then mirrored
  // onto the right 2 — the classic identicon trick for a shape that always
  // reads as a single coherent, roughly-symmetric mark rather than noise.
  const cells = [];
  for (let y = 0; y < 5; y++) {
    for (let x = 0; x < 3; x++) {
      if (rand() > 0.55) {
        cells.push([x, y]);
        if (x < 2) cells.push([4 - x, y]);
      }
    }
  }
  return { color, cells };
}
