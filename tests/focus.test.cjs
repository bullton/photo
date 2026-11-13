/* FocusCore sanity tests on generated images (no external dependencies). */
'use strict';

const assert = require('assert');
const Core = require('../focus-core.js');

const W = 640;
const H = 480;

function clamp(v, min, max) {
  return v < min ? min : v > max ? max : v;
}

function makeRandom(seed) {
  let state = seed >>> 0;
  return function () {
    state = (state * 1664525 + 1013904223) >>> 0;
    return state / 4294967296;
  };
}

/** Detailed scene: hard cell edges, lines and a soft light falloff. */
function makeDetailed(seed) {
  const random = makeRandom(seed);
  const luma = new Float32Array(W * H);
  for (let y = 0; y < H; y++) {
    for (let x = 0; x < W; x++) {
      const cell = ((x >> 2) + (y >> 2)) % 2 ? 46 : -34;
      const line = (x + y) % 41 < 2 ? 48 : 0;
      const ring = Math.abs(Math.hypot(x - W * 0.42, y - H * 0.46) - 130) < 2 ? -60 : 0;
      const light = 42 * Math.exp(-((x - W * 0.38) ** 2 + (y - H * 0.4) ** 2) / (2 * (W * 0.3) ** 2));
      luma[y * W + x] = clamp(122 + cell + line + ring + light + (random() - 0.5) * 10, 0, 255);
    }
  }
  return luma;
}

/** Sharp subject in the middle, smooth background. */
function makePortrait(seed) {
  const random = makeRandom(seed);
  const luma = new Float32Array(W * H);
  const cx = W * 0.5;
  const cy = H * 0.46;
  const radius = Math.min(W, H) * 0.28;
  for (let y = 0; y < H; y++) {
    for (let x = 0; x < W; x++) {
      const d = Math.hypot(x - cx, y - cy);
      let v = 120 + 18 * Math.sin(x / 90);
      if (d < radius) {
        const cell = ((x >> 2) + (y >> 2)) % 2 ? 52 : -38;
        const line = (x * 3 + y) % 29 < 2 ? 36 : 0;
        v += cell + line + (random() - 0.5) * 12;
      }
      luma[y * W + x] = clamp(v, 0, 255);
    }
  }
  return luma;
}

/** Slow gradient only: near-zero detail, should be flagged low texture. */
function makeFlat() {
  const luma = new Float32Array(W * H);
  for (let y = 0; y < H; y++) {
    for (let x = 0; x < W; x++) {
      luma[y * W + x] = 70 + (x / W) * 90 + (y / H) * 20;
    }
  }
  return luma;
}

/** Separable box blur; radius 0 returns the source unchanged. */
function boxBlur(source, radius) {
  if (radius <= 0) return source.slice();
  const tmp = new Float32Array(source.length);
  const out = new Float32Array(source.length);
  for (let y = 0; y < H; y++) {
    for (let x = 0; x < W; x++) {
      let sum = 0;
      let n = 0;
      for (let k = -radius; k <= radius; k++) {
        const xx = x + k;
        if (xx < 0 || xx >= W) continue;
        sum += source[y * W + xx];
        n++;
      }
      tmp[y * W + x] = sum / n;
    }
  }
  for (let y = 0; y < H; y++) {
    for (let x = 0; x < W; x++) {
      let sum = 0;
      let n = 0;
      for (let k = -radius; k <= radius; k++) {
        const yy = y + k;
        if (yy < 0 || yy >= H) continue;
        sum += tmp[yy * W + x];
        n++;
      }
      out[y * W + x] = sum / n;
    }
  }
  return out;
}

function analyze(luma) {
  return Core.analyzeLuma(luma, W, H);
}

function main() {
  const detailed = makeDetailed(20261109);
  const sharp = analyze(detailed);
  const medium = analyze(boxBlur(detailed, 2));
  const blurry = analyze(boxBlur(detailed, 7));
  const portrait = analyze(makePortrait(7788));
  const flat = analyze(makeFlat());

  const rows = [
    ['sharp', sharp],
    ['medium', medium],
    ['blurry', blurry],
    ['portrait', portrait],
    ['flat', flat]
  ];
  for (const [name, r] of rows) {
    console.log(
      `${name.padEnd(9)} raw=${r.score.toFixed(1).padStart(8)} global=${r.global
        .toFixed(1)
        .padStart(8)} center=${r.center.toFixed(1).padStart(8)} best=${r.best
        .toFixed(1)
        .padStart(7)} texture=${(r.textureRatio * 100).toFixed(0).padStart(3)}% gradP90=${r.gradP90.toFixed(2).padStart(5)} lowTex=${r.lowTexture}`
    );
  }

  assert.ok(sharp.score > blurry.score * 4, 'sharp must clearly beat blurred');
  assert.ok(sharp.score > medium.score, 'sharp must beat medium');
  assert.ok(medium.score > blurry.score, 'medium must beat blurred');
  assert.ok(flat.lowTexture === true, 'flat gradient must be low texture');
  assert.ok(sharp.lowTexture === false, 'detailed scene must not be low texture');
  assert.ok(portrait.lowTexture === false, 'portrait must not be low texture');
  assert.ok(portrait.center > portrait.global * 2, 'center subject must lift the center score');

  // Mirror the app: split raw scores at the widest ratio gap, then place the
  // threshold at the midpoint of the two adjacent normalized scores.
  function lineFor(rawScores) {
    const floor = Math.max(0, Math.min(100, Math.round(Core.normalizedPosition(Core.SOFT_FLOOR, rawScores))));
    const split = Core.findGapSplit(rawScores);
    if (split.index < 0) return floor;
    const norms = Core.normalizeScores(rawScores);
    const pairs = rawScores
      .map(function (raw, i) { return { raw: raw, norm: norms[i] }; })
      .sort(function (a, b) { return a.raw - b.raw; });
    const mid = (pairs[split.index].norm + pairs[split.index + 1].norm) / 2;
    return Math.max(floor, Math.max(0, Math.min(100, Math.round(mid))));
  }

  const raws = [sharp.score, medium.score, blurry.score, portrait.score, flat.score];
  const norms = Core.normalizeScores(raws);
  const line = lineFor(raws);
  const labels = norms.map(function (n, i) {
    return Core.verdict(n, line, rows[i][1].lowTexture);
  });
  console.log('\nnorms=' + norms.map(function (n) { return n.toFixed(0); }).join(',') + '  line=' + line);
  console.log('labels=' + labels.join(','));

  assert.ok(labels[0] === 'sharp', 'sharp scene should classify as sharp');
  assert.ok(norms[2] < line, 'blurred scene must sit below the line');
  assert.ok(labels[2] !== 'sharp', 'blurred scene must never classify as sharp');
  assert.ok(labels[4] === 'border', 'flat gradient should stay protected as border');

  // Laplacian variance spans orders of magnitude in real photos: soft frames
  // sit far below the sharp cluster, so the wide ratio gap is the cluster edge.
  const realistic = lineFor([880, 840, 800, 760, 720, 680, 40, 35, 12, 8]);
  console.log('realistic line=' + realistic);
  assert.ok(realistic > 40 && realistic < 90, 'gap split must separate soft shots from sharp ones');

  // Absolute floor used to keep a continuous batch from collapsing to line 0.
  const batch = [5, 12, 26, 55, 85, 140, 300, 700, 1200];
  const floorPos = Core.normalizedPosition(Core.SOFT_FLOOR, batch);
  console.log('floor=' + Core.SOFT_FLOOR + '  floorPos=' + floorPos.toFixed(1));
  assert.ok(floorPos > 0 && floorPos < 100, 'floor must sit inside the batch scale');
  assert.ok(Core.normalizedPosition(5, batch) < floorPos, 'score below floor sits lower');
  assert.ok(Core.normalizedPosition(1200, batch) > floorPos, 'score above floor sits higher');
  assert.strictEqual(Core.normalizedPosition(50, [50, 50]), 0, 'no-spread batch sits at 0');

  // Exposure metrics ride on the same luma pass.
  assert.ok(Number.isFinite(sharp.meanLuma), 'mean luma must be reported');
  assert.ok(sharp.highlightClip >= 0 && sharp.highlightClip <= 1, 'highlight clip is a ratio');
  assert.ok(sharp.shadowClip >= 0 && sharp.shadowClip <= 1, 'shadow clip is a ratio');
  assert.strictEqual(Core.classifyExposure(120, 0.01, 0.02), 'normal');
  assert.strictEqual(Core.classifyExposure(200, 0.12, 0.0), 'over');
  assert.strictEqual(Core.classifyExposure(12, 0.0, 0.6), 'under');

  // Slow-shutter guard: 1/15s @420mm is a smear even when water texture keeps
  // the sharpness score high; short exposures and normal lenses stay untouched.
  assert.strictEqual(Core.slowShutter(1 / 15, 420), true);
  assert.strictEqual(Core.slowShutter(1 / 40, 600), true);
  assert.strictEqual(Core.slowShutter(1 / 100, 600), false);
  assert.strictEqual(Core.slowShutter(1 / 15, 50), false);
  assert.strictEqual(Core.slowShutter(0, 420), false);
  assert.strictEqual(Core.verdictWithExif(90, 50, false, 1 / 320, 420), 'sharp');
  assert.strictEqual(Core.verdictWithExif(90, 50, false, 1 / 15, 420), 'border');

  const seq = Core.parseShotSequence('DSC_8715.NEF');
  assert.strictEqual(seq.prefix, 'dsc_');
  assert.strictEqual(seq.number, 8715);
  assert.strictEqual(Core.parseShotSequence('IMG_0042.JPG').number, 42);
  assert.strictEqual(Core.parseShotSequence('DSC_8576_DxO.jpg').number, 8576);
  assert.strictEqual(Core.parseShotSequence('DSC_8576_DxO.jpg').prefix, 'dsc_');
  assert.strictEqual(Core.parseShotSequence('no-number').number, null);

  // Perceptual hash: identical frames match exactly, a noisy variant stays
  // much closer than an unrelated scene.
  const noisy = Float32Array.from(detailed, function (v, i) {
    return clamp(v + (((i * 2654435761) >>> 0) % 100) / 100 * 6 - 3, 0, 255);
  });
  const hashA = Core.hashLuma(detailed, W, H);
  const hashB = Core.hashLuma(noisy, W, H);
  const hashC = Core.hashLuma(makePortrait(7788), W, H);
  assert.strictEqual(hashA.length, 16, 'hash is 64 bits packed into 16 hex chars');
  assert.strictEqual(Core.hashDistance(hashA, hashA), 0, 'identical hashes have zero distance');
  assert.ok(Core.hashDistance(hashA, hashB) < Core.hashDistance(hashA, hashC), 'noisy variant must be closer than another scene');

  const still = function (id, name, hash, order, mean, color) {
    return { id: id, name: name, hash: hash, order: order, width: 6000, height: 4000, mean: mean == null ? 50 : mean, color: color || null };
  };
  const zero = '0000000000000000';
  const far = 'ffffffffffffffff';

  const burst = Core.groupPhotos([
    still('a', 'DSC_100.NEF', zero, 1),
    still('b', 'DSC_101.NEF', zero, 2),
    still('c', 'DSC_102.NEF', zero, 3),
    still('d', 'DSC_103.NEF', far, 4)
  ], { sensitivity: 'standard' });
  assert.deepStrictEqual(burst, [['a', 'b', 'c'], ['d']], 'contiguous identical frames join, a different frame splits');

  const near = '0000000000000fff'; // Hamming distance 12 from zero
  const pair = function (sensitivity) {
    return Core.groupPhotos([
      still('x', 'X_10.NEF', zero, 1),
      still('y', 'X_15.NEF', near, 2)
    ], { sensitivity: sensitivity });
  };
  assert.strictEqual(pair('strict').length, 2, 'strict grouping keeps a 5-frame gap apart');
  assert.strictEqual(pair('standard').length, 2, 'standard grouping needs the frames to be very close');
  assert.strictEqual(pair('loose').length, 1, 'loose grouping tolerates a wider gap');

  // A frame that clearly matches its neighbour must not be dropped just
  // because older frames in the window differ (the old 2 + 1 split). Here the
  // last frame is 16 bits from its neighbour but far from the first two, so an
  // average would reject it while the nearest-neighbour rule keeps it.
  const bridge = Core.groupPhotos([
    still('n1', 'N_200.NEF', '0000000000000000', 1),
    still('n2', 'N_201.NEF', '000000000000ffff', 2),
    still('n3', 'N_202.NEF', '00000000ffffffff', 3),
    still('n4', 'N_203.NEF', '0000ffffffffffff', 4)
  ], { sensitivity: 'standard' });
  assert.deepStrictEqual(bridge, [['n1', 'n2', 'n3', 'n4']], 'nearest-neighbour match keeps a short burst together');

  // Very different brightness means a different scene even when the edges
  // happen to look alike; a small exposure shift inside a burst still joins.
  assert.strictEqual(Core.groupPhotos([
    still('b1', 'B_300.NEF', zero, 1, 40),
    still('b2', 'B_301.NEF', zero, 2, 120)
  ], { sensitivity: 'standard' }).length, 2, 'brightness jump splits the scene');
  assert.strictEqual(Core.groupPhotos([
    still('c1', 'C_400.NEF', zero, 1, 40),
    still('c2', 'C_401.NEF', zero, 2, 55)
  ], { sensitivity: 'standard' }).length, 1, 'small exposure drift stays in one group');

  // Colour layout separates different scenes that a soft luminance hash would
  // otherwise merge (green plants vs. a wooden bench vs. open water).
  const red = [255, 0, 0];
  const blue = [0, 0, 255];
  assert.strictEqual(Core.groupPhotos([
    still('d1', 'D_500.NEF', zero, 1, 50, red),
    still('d2', 'D_501.NEF', zero, 2, 50, blue)
  ], { sensitivity: 'standard' }).length, 2, 'different colours split the scene');
  assert.strictEqual(Core.groupPhotos([
    still('e1', 'E_600.NEF', zero, 1, 50, red),
    still('e2', 'E_601.NEF', zero, 2, 50, red)
  ], { sensitivity: 'standard' }).length, 1, 'same colours stay together');

  // EXIF time clustering: unsorted frames are ordered by capture time, a long
  // pause starts a new burst, and frames without a timestamp still fall back
  // to one filename-ordered cluster.
  const timed = function (id, time, order) {
    return { id: id, name: id + '.NEF', time: time, order: order };
  };
  const clusters = Core.timeCluster([
    timed('t5', 8500, 5),
    timed('t3', 1900, 3),
    timed('t1', 1000, 1),
    timed('t4', 8000, 4),
    timed('t2', 1300, 2)
  ], { threshold: 2000 });
  assert.deepStrictEqual(
    clusters.map(function (c) { return c.map(function (i) { return i.id; }); }),
    [['t1', 't2', 't3'], ['t4', 't5']],
    'capture time sorts frames and a pause starts a new burst'
  );
  assert.deepStrictEqual(Core.timeCluster([], {}), [], 'empty input has no clusters');
  const noTime = Core.timeCluster([
    { id: 'u2', name: 'U_2.NEF', order: 2 },
    { id: 'u1', name: 'U_1.NEF', order: 1 }
  ], {});
  assert.deepStrictEqual(
    noTime.map(function (c) { return c.map(function (i) { return i.id; }); }),
    [['u1', 'u2']],
    'frames without time still take part as one cluster'
  );

  const rgba = new Uint8ClampedArray(8 * 8 * 4);
  for (let y = 0; y < 8; y++) {
    for (let x = 0; x < 8; x++) {
      const p = (y * 8 + x) * 4;
      const right = x >= 4;
      rgba[p] = right ? 0 : 255;
      rgba[p + 1] = 0;
      rgba[p + 2] = right ? 255 : 0;
      rgba[p + 3] = 255;
    }
  }
  const grid = Core.colorGrid(rgba, 8, 8, 2);
  assert.strictEqual(grid.length, 12, 'color grid is size*size*3');
  assert.ok(Core.colorDistance(grid, grid) === 0, 'identical grids have zero distance');

  // Absolute score: fixed anchors, independent of the rest of the batch.
  assert.strictEqual(Math.round(Core.absoluteScore(Core.SOFT_FLOOR)), 50, 'soft floor maps to 50');
  assert.strictEqual(Core.absoluteScore(0), 0, 'zero variance maps to 0');
  assert.strictEqual(Core.absoluteScore(1e9), 100, 'huge variance clamps to 100');
  assert.ok(Core.absoluteScore(300) > 60, 'sharp frame scores above the line');
  assert.ok(Core.absoluteScore(40) < 40, 'soft frame scores below the line');
  assert.ok(
    Core.absoluteScore(600) > Core.absoluteScore(300) &&
      Core.absoluteScore(300) > Core.absoluteScore(110),
    'absolute score is monotonic'
  );
  assert.strictEqual(Core.SCORE_LINE, 50, 'default sharp line is 50');

  console.log('\nAll focus-core tests passed.');
}

main();
