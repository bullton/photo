/* FocusCore — local focus scoring shared by the page and the test script. */
(function (root, factory) {
  if (typeof module === 'object' && module.exports) {
    module.exports = factory();
  } else {
    root.FocusCore = factory();
  }
})(typeof self !== 'undefined' ? self : this, function () {
  'use strict';

  function clamp(value, min, max) {
    return value < min ? min : value > max ? max : value;
  }

  function lumaFromRGBA(rgba, width, height) {
    const count = width * height;
    const luma = new Float32Array(count);
    for (let i = 0, p = 0; i < count; i++, p += 4) {
      luma[i] = 0.2126 * rgba[p] + 0.7152 * rgba[p + 1] + 0.0722 * rgba[p + 2];
    }
    return luma;
  }

  function varianceOfRange(source, width, x0, y0, x1, y1) {
    let sum = 0;
    let sumSq = 0;
    let n = 0;
    for (let y = y0; y < y1; y++) {
      const row = y * width;
      for (let x = x0; x < x1; x++) {
        const v = source[row + x];
        sum += v;
        sumSq += v * v;
        n++;
      }
    }
    if (n < 2) return 0;
    const mean = sum / n;
    return Math.max(0, sumSq / n - mean * mean);
  }

  function percentile(sorted, p) {
    if (!sorted.length) return 0;
    const index = clamp((p / 100) * (sorted.length - 1), 0, sorted.length - 1);
    const low = Math.floor(index);
    const high = Math.ceil(index);
    if (low === high) return sorted[low];
    const weight = index - low;
    return sorted[low] * (1 - weight) + sorted[high] * weight;
  }

  /**
   * Score one luminance plane.
   * The raw score is the variance of the 4-neighbour Laplacian over the whole
   * frame and the central 62% crop, whichever is higher.
   */
  function analyzeLuma(luma, width, height, options) {
    options = options || {};
    if (!width || !height || width * height !== luma.length) {
      throw new Error('invalid luma plane');
    }

    const blockSize = Math.max(6, Math.round(options.blockSize || 16));
    const lap = new Float32Array(width * height);

    if (width > 2 && height > 2) {
      for (let y = 1; y < height - 1; y++) {
        const row = y * width;
        for (let x = 1; x < width - 1; x++) {
          const c = row + x;
          lap[c] =
            luma[c - 1] + luma[c + 1] + luma[c - width] + luma[c + width] - 4 * luma[c];
        }
      }
    }

    const globalVar = varianceOfRange(
      lap,
      width,
      1,
      1,
      Math.max(2, width - 1),
      Math.max(2, height - 1)
    );

    const cropW = Math.max(4, Math.round(width * 0.62));
    const cropH = Math.max(4, Math.round(height * 0.62));
    const cropX = Math.max(1, Math.floor((width - cropW) / 2));
    const cropY = Math.max(1, Math.floor((height - cropH) / 2));
    const centerVar = varianceOfRange(
      lap,
      width,
      cropX,
      cropY,
      Math.min(width - 1, cropX + cropW),
      Math.min(height - 1, cropY + cropH)
    );

    const cols = Math.max(1, Math.ceil(Math.max(1, width - 2) / blockSize));
    const rows = Math.max(1, Math.ceil(Math.max(1, height - 2) / blockSize));
    const blockVars = new Float32Array(cols * rows);
    const blockGrads = new Float32Array(cols * rows);
    // Four-direction gradient energy (0/45/90/135°). A motion-blurred frame
    // loses detail along the smear axis, so one direction collapses; a sharp
    // frame keeps all four populated.
    const dirEnergy = [0, 0, 0, 0];

    for (let by = 0; by < rows; by++) {
      for (let bx = 0; bx < cols; bx++) {
        const px0 = 1 + bx * blockSize;
        const py0 = 1 + by * blockSize;
        const px1 = Math.min(width - 1, px0 + blockSize);
        const py1 = Math.min(height - 1, py0 + blockSize);
        let sum = 0;
        let sumSq = 0;
        let gradSum = 0;
        let n = 0;
        for (let y = py0; y < py1; y++) {
          const row = y * width;
          const up = row - width;
          const down = row + width;
          for (let x = px0; x < px1; x++) {
            const c = row + x;
            const v = lap[c];
            sum += v;
            sumSq += v * v;
            const gx = luma[c + 1] - luma[c - 1];
            const gy = luma[down + x] - luma[up + x];
            gradSum += Math.sqrt(gx * gx + gy * gy) * 0.5;
            dirEnergy[0] += gx * gx;
            dirEnergy[1] += (gx * gx + 2 * gx * gy + gy * gy) * 0.5;
            dirEnergy[2] += gy * gy;
            dirEnergy[3] += (gx * gx - 2 * gx * gy + gy * gy) * 0.5;
            n++;
          }
        }
        const index = by * cols + bx;
        if (n > 3) {
          const mean = sum / n;
          blockVars[index] = Math.max(0, sumSq / n - mean * mean);
          blockGrads[index] = gradSum / n;
        }
      }
    }

    const sortedVars = Array.from(blockVars).sort(function (a, b) {
      return a - b;
    });
    const best = percentile(sortedVars, 90);
    const p95 = percentile(sortedVars, 95);

    let textured = 0;
    for (let i = 0; i < blockGrads.length; i++) {
      if (blockGrads[i] > 3.5) textured++;
    }
    const textureRatio = blockGrads.length ? textured / blockGrads.length : 0;
    const sortedGrads = Array.from(blockGrads).sort(function (a, b) {
      return a - b;
    });
    const gradP90 = percentile(sortedGrads, 90);
    const lowTexture = textureRatio < 0.1 && gradP90 < 2.2;

    const map = new Uint8Array(cols * rows);
    const denominator = Math.log1p(p95);
    if (denominator > 0) {
      for (let i = 0; i < blockVars.length; i++) {
        const value = clamp(Math.log1p(blockVars[i]) / denominator, 0, 1);
        map[i] = Math.round(value * 255);
      }
    }

    // Exposure raw material: mean brightness plus how much of the frame piles
    // up at the white and black ends. Kept on the same pass so the page can
    // flag clipped frames without a second scan.
    let lumaSum = 0;
    let highlightCount = 0;
    let shadowCount = 0;
    for (let i = 0; i < luma.length; i++) {
      const v = luma[i];
      lumaSum += v;
      if (v >= 250) highlightCount++;
      else if (v <= 4) shadowCount++;
    }
    const pixelCount = luma.length || 1;
    const dirMax = Math.max(dirEnergy[0], dirEnergy[1], dirEnergy[2], dirEnergy[3]);
    const dirMin = Math.min(dirEnergy[0], dirEnergy[1], dirEnergy[2], dirEnergy[3]);
    const directionalRatio = dirMax > 1e-9 ? dirMin / dirMax : 1;

    return {
      score: Math.max(globalVar, centerVar),
      global: globalVar,
      center: centerVar,
      best: best,
      meanLuma: lumaSum / pixelCount,
      highlightClip: highlightCount / pixelCount,
      shadowClip: shadowCount / pixelCount,
      textureRatio: textureRatio,
      gradP90: gradP90,
      directionalRatio: directionalRatio,
      lowTexture: lowTexture,
      blockSize: blockSize,
      cols: cols,
      rows: rows,
      blockVars: blockVars,
      map: map,
      width: width,
      height: height
    };
  }

  /** Map raw scores to 0–100 within the current batch (log scale). */
  /**
   * Absolute 0-100 score for a raw subject-sharpness value.
   *
   * Batch-relative normalization made a photo's score (and therefore its
   * verdict and stars) depend on which other photos were imported with it:
   * the same frame could read 4 stars today and 3 next week. This fixed log
   * curve anchors SOFT_FLOOR (110, empirically the soft/practical boundary
   * at 1600px) at 50 and a solidly sharp frame (~760) at 100.
   */
  function absoluteScore(rawScore) {
    const value = Math.max(0, Number.isFinite(rawScore) ? rawScore : 0);
    const low = Math.log1p(15);
    const high = Math.log1p(760);
    if (high - low < 1e-9) return 0;
    return clamp((100 * (Math.log1p(value) - low)) / (high - low), 0, 100);
  }

  function normalizeScores(rawScores) {
    const values = rawScores.map(function (value) {
      return Math.max(0, Number.isFinite(value) ? value : 0);
    });
    if (!values.length) return [];
    let min = Infinity;
    let max = 0;
    for (let i = 0; i < values.length; i++) {
      if (values[i] < min) min = values[i];
      if (values[i] > max) max = values[i];
    }
    const low = Math.log1p(min);
    const high = Math.log1p(max);
    if (high - low < 1e-9) return values.map(function () { return 100; });
    return values.map(function (value) {
      return (100 * (Math.log1p(value) - low)) / (high - low);
    });
  }

  /**
   * Where one raw score sits on the same 0–100 log scale as normalizeScores,
   * used to anchor an absolute subject-sharpness floor inside a batch.
   * Returns 0 when the batch has no spread.
   */
  function normalizedPosition(value, rawScores) {
    if (!rawScores || !rawScores.length) return 0;
    var min = Infinity;
    var max = 0;
    for (var i = 0; i < rawScores.length; i++) {
      var v = Number.isFinite(rawScores[i]) ? Math.max(0, rawScores[i]) : 0;
      if (v < min) min = v;
      if (v > max) max = v;
    }
    var low = Math.log1p(min);
    var high = Math.log1p(max);
    if (high - low < 1e-9) return 0;
    var position = (100 * (Math.log1p(Math.max(0, value)) - low)) / (high - low);
    return clamp(position, 0, 100);
  }

  /**
   * Find the strongest ratio gap between sorted raw scores.
   * Returns { index, ratio, low, high } where index is the gap's lower item,
   * or index -1 when no gap is meaningful (a uniform batch stays undivided).
   * Gaps that would isolate just the top of a tight batch need a larger ratio,
   * so a single unusually textured frame does not flag the rest as soft.
   */
  function findGapSplit(rawScores, options) {
    options = options || {};
    var baseRatio = Number.isFinite(options.minRatio) ? options.minRatio : 2.2;
    var tailRatio = Number.isFinite(options.tailRatio) ? options.tailRatio : 3.5;
    var items = rawScores.map(function (value, index) {
      return { value: Math.max(0, Number.isFinite(value) ? value : 0), index: index };
    }).sort(function (a, b) { return a.value - b.value; });

    if (items.length < 2) return { index: -1, ratio: 1, low: 0, high: 0 };
    var n = items.length;
    // Winsorize unusually sharp frames at 2x the median before measuring gaps.
    // A single razor-sharp macro can otherwise define the split and push the
    // line above ordinary in-focus shots in the same batch.
    var sortedValues = items.map(function (item) { return item.value; });
    var median = sortedValues[Math.floor((n - 1) / 2)];
    if (n % 2 === 0) median = (sortedValues[n / 2 - 1] + sortedValues[n / 2]) / 2;
    var cap = median > 0 ? median * 2 : Infinity;
    var best = { index: -1, ratio: 1, low: 0, high: 0 };
    for (var i = 0; i < n - 1; i++) {
      var low = Math.min(items[i].value, cap);
      var high = Math.min(items[i + 1].value, cap);
      var ratio = (high + 1) / (low + 1);
      var lowerCount = i + 1;
      var required = lowerCount / n > 0.8 ? tailRatio : baseRatio;
      if (ratio < required) continue;
      if (ratio > best.ratio) best = { index: i, ratio: ratio, low: low, high: high };
    }
    return best;
  }

  /**
   * Score sharpness inside a normalized box (x0,y0,x1,y1 in 0..1) using the
   * raw per-block Laplacian variances from analyzeLuma. Returns aggregated
   * subject-region sharpness so the AF area can be judged on its own.
   */
  function scoreBox(analysis, box) {
    if (!analysis || !analysis.blockVars || !analysis.cols || !analysis.rows || !box) return null;
    var cols = analysis.cols;
    var rows = analysis.rows;
    var size = analysis.blockSize;
    var w = analysis.width;
    var h = analysis.height;
    if (!w || !h) return null;
    var x0 = clamp(box.x0, 0, 1) * w;
    var x1 = clamp(box.x1, 0, 1) * w;
    var y0 = clamp(box.y0, 0, 1) * h;
    var y1 = clamp(box.y1, 0, 1) * h;
    var values = [];
    for (var by = 0; by < rows; by++) {
      var cy = 1 + by * size + size / 2;
      if (cy < y0 || cy > y1) continue;
      for (var bx = 0; bx < cols; bx++) {
        var cx = 1 + bx * size + size / 2;
        if (cx < x0 || cx > x1) continue;
        values.push(analysis.blockVars[by * cols + bx]);
      }
    }
    if (!values.length) {
      var tx = (x0 + x1) / 2;
      var ty = (y0 + y1) / 2;
      var bestIndex = -1;
      var bestDist = Infinity;
      for (var ry = 0; ry < rows; ry++) {
        for (var rx = 0; rx < cols; rx++) {
          var px = 1 + rx * size + size / 2;
          var py = 1 + ry * size + size / 2;
          var dist = (px - tx) * (px - tx) + (py - ty) * (py - ty);
          if (dist < bestDist) {
            bestDist = dist;
            bestIndex = ry * cols + rx;
          }
        }
      }
      if (bestIndex < 0) return null;
      values.push(analysis.blockVars[bestIndex]);
    }
    values.sort(function (a, b) { return a - b; });
    var sum = 0;
    for (var i = 0; i < values.length; i++) sum += values[i];
    var mean = sum / values.length;
    var p25 = percentile(values, 25);
    var p50 = percentile(values, 50);
    var p75 = percentile(values, 75);
    return {
      mean: mean,
      p25: p25,
      p50: p50,
      p75: p75,
      count: values.length,
      best: values[values.length - 1]
    };
  }

  /**
   * Coarse exposure class from the luma pass. Only three buckets so it can act
   * as a gentle tie-breaker inside a burst without overruling sharpness.
   */
  function classifyExposure(meanLuma, highlightClip, shadowClip) {
    const hi = Number.isFinite(highlightClip) ? highlightClip : 0;
    const lo = Number.isFinite(shadowClip) ? shadowClip : 0;
    const mid = Number.isFinite(meanLuma) ? meanLuma : 128;
    if (hi >= 0.08) return 'over';
    if (mid <= 30 && lo >= 0.3) return 'under';
    return 'normal';
  }

  /** Split a filename into a prefix and its last shot number: DSC_8715.NEF -> DSC_/8715. */
  function parseShotSequence(name) {
    const text = String(name == null ? '' : name);
    const match = /^(.*\D)?(\d+)(?:[^\d].*)?$/.exec(text);
    if (!match) return { prefix: text.toLowerCase(), number: null };
    return { prefix: (match[1] || '').toLowerCase(), number: parseInt(match[2], 10) };
  }

  /**
   * Difference hash (dHash) on a 9x8 grid: 64 bits packed into 16 hex chars.
   * Similar framings produce close hashes, which is all burst grouping needs.
   */
  function hashLuma(luma, width, height) {
    if (!luma || !width || !height || width * height !== luma.length) return '';
    const gw = 9;
    const gh = 8;
    const cells = new Float64Array(gw * gh);
    const counts = new Uint32Array(gw * gh);
    for (let y = 0; y < height; y++) {
      const cy = Math.min(gh - 1, Math.floor((y * gh) / height));
      const row = y * width;
      for (let x = 0; x < width; x++) {
        const cx = Math.min(gw - 1, Math.floor((x * gw) / width));
        const index = cy * gw + cx;
        cells[index] += luma[row + x];
        counts[index]++;
      }
    }
    let bits = '';
    for (let y = 0; y < gh; y++) {
      for (let x = 0; x < gw - 1; x++) {
        const index = y * gw + x;
        const left = counts[index] ? cells[index] / counts[index] : 0;
        const right = counts[index + 1] ? cells[index + 1] / counts[index + 1] : 0;
        bits += left < right ? '1' : '0';
      }
    }
    let hex = '';
    for (let i = 0; i < bits.length; i += 4) {
      hex += parseInt(bits.slice(i, i + 4), 2).toString(16);
    }
    return hex;
  }

  /**
   * Coarse colour layout: a size×size grid of mean RGB. This survives softness
   * (blur does not change colours) and separates scenes that a luminance hash
   * lumps together, such as green plants versus a wooden bench or open water.
   */
  function colorGrid(rgba, width, height, size) {
    size = Math.max(2, Math.round(size || 6));
    if (!rgba || !width || !height) return null;
    const cells = new Uint32Array(size * size * 3);
    const counts = new Uint32Array(size * size);
    for (let y = 0; y < height; y++) {
      const cy = Math.min(size - 1, Math.floor((y * size) / height));
      const row = y * width;
      for (let x = 0; x < width; x++) {
        const cx = Math.min(size - 1, Math.floor((x * size) / width));
        const index = cy * size + cx;
        const p = (row + x) * 4;
        cells[index * 3] += rgba[p];
        cells[index * 3 + 1] += rgba[p + 1];
        cells[index * 3 + 2] += rgba[p + 2];
        counts[index]++;
      }
    }
    const out = new Uint8Array(size * size * 3);
    for (let i = 0; i < size * size; i++) {
      const n = counts[i] || 1;
      out[i * 3] = Math.round(cells[i * 3] / n);
      out[i * 3 + 1] = Math.round(cells[i * 3 + 1] / n);
      out[i * 3 + 2] = Math.round(cells[i * 3 + 2] / n);
    }
    return out;
  }

  /** Mean absolute per-channel difference between two colorGrid outputs. */
  function colorDistance(a, b) {
    if (!a || !b || a.length !== b.length) return 0;
    let sum = 0;
    for (let i = 0; i < a.length; i++) sum += Math.abs(a[i] - b[i]);
    return sum / a.length;
  }

  const POPCOUNT = [0, 1, 1, 2, 1, 2, 2, 3, 1, 2, 2, 3, 2, 3, 3, 4];

  /** Hamming distance between two hashes from hashLuma (0–64). */
  function hashDistance(a, b) {
    if (!a || !b) return 64;
    const n = Math.min(a.length, b.length);
    let distance = 0;
    for (let i = 0; i < n; i++) {
      const x = parseInt(a[i], 16) ^ parseInt(b[i], 16);
      distance += POPCOUNT[x] || 0;
    }
    return distance + Math.abs(a.length - b.length) * 4;
  }

  const GROUP_CONFIG = {
    strict: { base: 10, gap: 2, maxGap: 4, veryClose: 0.55, bright: 16, color: 16 },
    standard: { base: 18, gap: 4, maxGap: 12, veryClose: 0.6, bright: 22, color: 24 },
    loose: { base: 22, gap: 6, maxGap: 24, veryClose: 0.66, bright: 30, color: 38 },
    veryLoose: { base: 30, gap: 10, maxGap: 60, veryClose: 0.8, bright: 40, color: 56 }
  };

  function similarAspect(a, b) {
    if (!a || !b || !a.width || !a.height || !b.width || !b.height) return true;
    const ra = a.width / a.height;
    const rb = b.width / b.height;
    return Math.abs(ra - rb) / Math.max(ra, rb) <= 0.05;
  }

  function brightnessCompatible(a, b, gate) {
    return Math.abs((a.mean || 0) - (b.mean || 0)) <= gate;
  }

  function colorCompatible(a, color, gate) {
    if (!a.color || !color) return true;
    return colorDistance(a.color, color) <= gate;
  }

  // Compare against the closest of the last few frames, not their average.
  // Averaging lets one slightly different frame in the window push a frame that
  // clearly matches its neighbour out of the group, which split short bursts.
  // Brightness only gates frames that are already structurally close, so a
  // distant framing and a close-up of the same birds do not collapse together.
  function nearestHashDistance(group, hash, mean, color, config) {
    const span = Math.min(group.length, 4);
    let best = 64;
    for (let i = group.length - span; i < group.length; i++) {
      if (!brightnessCompatible(group[i], { mean: mean }, config.bright)) continue;
      if (!colorCompatible(group[i], color, config.color)) continue;
      const distance = hashDistance(group[i].hash, hash);
      if (distance < best) best = distance;
    }
    return best;
  }

  function shotGap(a, b) {
    if (a.number != null && b.number != null && a.prefix === b.prefix) {
      return Math.abs(a.number - b.number);
    }
    return Math.abs(a.order - b.order);
  }

  /**
   * First-pass burst detection on capture time: frames taken within
   * `threshold` milliseconds of the previous frame form one suspected burst.
   * Each cluster is then compared on its own, so a scene shot minutes later
   * can never borrow a group from an earlier lookalike. Frames with no EXIF
   * timestamp are pooled into a single trailing cluster, which keeps the
   * filename-based grouping working for formats that carry no capture time.
   * Returns arrays of the original item objects.
   */
  function timeCluster(items, options) {
    options = options || {};
    const threshold = Number(options.threshold) > 0 ? Number(options.threshold) : 2000;
    const list = (items || []).filter(function (item) {
      return item && item.id;
    });
    const timed = [];
    const untimed = [];
    list.forEach(function (item) {
      if (Number.isFinite(item.time)) timed.push(item);
      else untimed.push(item);
    });
    timed.sort(function (a, b) {
      if (a.time !== b.time) return a.time - b.time;
      return (Number(a.order) || 0) - (Number(b.order) || 0);
    });
    const clusters = [];
    let current = null;
    let lastTime = null;
    timed.forEach(function (item) {
      if (current && item.time - lastTime <= threshold) {
        current.push(item);
      } else {
        current = [item];
        clusters.push(current);
      }
      lastTime = item.time;
    });
    if (untimed.length) {
      untimed.sort(function (a, b) {
        return (Number(a.order) || 0) - (Number(b.order) || 0);
      });
      clusters.push(untimed);
    }
    return clusters;
  }



  /**
   * Cluster near-identical frames into groups. Items are ordered by filename
   * sequence first, then joined while the perceptual hash stays close to the
   * nearest recent frame and the shot number is contiguous (with a looser
   * allowance for very close frames). Returns arrays of ids in sequence order.
   */
  function groupPhotos(items, options) {
    options = options || {};
    const config = GROUP_CONFIG[options.sensitivity] || GROUP_CONFIG.standard;
    const list = (items || []).filter(function (item) {
      return item && item.id;
    }).map(function (item) {
      const seq = parseShotSequence(item.name);
      return {
        id: item.id,
        name: item.name || '',
        hash: item.hash || '',
        order: Number(item.order) || 0,
        width: Number(item.width) || 0,
        height: Number(item.height) || 0,
        mean: Number.isFinite(item.mean) ? item.mean : 0,
        color: item.color || null,
        prefix: seq.prefix,
        number: seq.number
      };
    });

    list.sort(function (a, b) {
      if (a.prefix !== b.prefix) return a.prefix < b.prefix ? -1 : 1;
      if (a.number != null && b.number != null && a.number !== b.number) return a.number - b.number;
      return a.order - b.order;
    });

    const groups = [];
    let current = null;
    for (let i = 0; i < list.length; i++) {
      const item = list[i];
      if (!current) {
        current = [item];
        groups.push(current);
        continue;
      }
      const prev = current[current.length - 1];
      const gap = shotGap(prev, item);
      const distance = similarAspect(prev, item)
        ? nearestHashDistance(current, item.hash, item.mean, item.color, config)
        : 64;
      const contiguous = distance <= config.base && gap <= config.gap;
      const veryClose = distance <= config.base * config.veryClose && gap <= config.maxGap;
      if (contiguous || veryClose) {
        current.push(item);
      } else {
        current = [item];
        groups.push(current);
      }
    }
    return groups.map(function (group) {
      return group.map(function (item) { return item.id; });
    });
  }

  function verdict(norm, line, lowTexture) {
    if (!Number.isFinite(norm)) return 'pending';
    if (lowTexture) return 'border';
    if (norm >= line) return 'sharp';
    if (norm >= line * 0.78) return 'border';
    return 'blur';
  }

  // 慢门拖影兜底：1/15s 配 420mm 时船体整体糊掉，但水面纹理仍撑高方差，
  // 单看清晰度会误判成合焦。等效焦距 ≥150mm 且比 1/焦距 再慢 3 档时认定为风险。
  function slowShutter(exposureSec, focal35) {
    var seconds = Number(exposureSec);
    var focal = Number(focal35);
    if (!Number.isFinite(seconds) || !Number.isFinite(focal)) return false;
    if (seconds <= 0 || focal < 150) return false;
    return seconds >= 8 / focal;
  }

  // 慢门只是“可能糊”：三脚架上的静止主体、单向线条都会骗人。只有快门慢、
  // 且四向梯度能量里确实有一个方向塌陷（单方向拖影）时才降级。
  function motionSmear(directionalRatio, exposureSec, focal35) {
    var ratio = Number(directionalRatio);
    if (!Number.isFinite(ratio)) return false;
    return ratio < 0.25 && slowShutter(exposureSec, focal35);
  }

  function verdictWithExif(norm, line, lowTexture, exposureSec, focal35, directionalRatio) {
    var value = verdict(norm, line, lowTexture);
    if (value === 'sharp' && motionSmear(directionalRatio, exposureSec, focal35)) return 'border';
    return value;
  }

  return {
    version: '1.1.0',
    // Subject sharpness (block-variance median at 1600px) below this reads as
    // soft in practice; also the 50-point anchor of absoluteScore.
    SOFT_FLOOR: 110,
    // Default sharp/border line on the absolute 0-100 scale.
    SCORE_LINE: 50,
    absoluteScore: absoluteScore,
    lumaFromRGBA: lumaFromRGBA,
    analyzeLuma: analyzeLuma,
    normalizeScores: normalizeScores,
    normalizedPosition: normalizedPosition,
    findGapSplit: findGapSplit,
    scoreBox: scoreBox,
    classifyExposure: classifyExposure,
    parseShotSequence: parseShotSequence,
    hashLuma: hashLuma,
    hashDistance: hashDistance,
    colorGrid: colorGrid,
    colorDistance: colorDistance,
    timeCluster: timeCluster,
    groupPhotos: groupPhotos,
    slowShutter: slowShutter,
    motionSmear: motionSmear,
    verdict: verdict,
    verdictWithExif: verdictWithExif
  };
});
