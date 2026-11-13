/* AIEnhance — 本地 AI 评分 / 自动标签的前端接线。
 *
 * 只负责三件事：
 * 1. 把已完成的照片路径发给本地分析服务，拿回分项分与标签；
 * 2. 在卡片和大图上补一个可解释的评分块；
 * 3. 按建议分批量打星（尊重手动打星）。
 *
 * 服务不可用（网页版直接打开、或未安装依赖）时全部静默降级。
 */
(function (root) {
  'use strict';

  var STYLE_ID = 'ai-enhance-style';
  var BATCH = 8;

  function params() {
    try { return new URLSearchParams(location.search); } catch (error) { return null; }
  }

  function token() {
    var search = params();
    return search ? (search.get('token') || '') : '';
  }

  function endpoint(path) {
    var value = token();
    return path + (value ? ('?token=' + encodeURIComponent(value)) : '');
  }

  function available() {
    return !!token() && !!root.fetch;
  }

  function pathOf(photo) {
    if (!photo) return '';
    if (photo.file && photo.file.__path) return photo.file.__path;
    if (photo.__path) return photo.__path;
    return '';
  }

  function injectStyles() {
    if (document.getElementById(STYLE_ID)) return;
    var style = document.createElement('style');
    style.id = STYLE_ID;
    style.textContent = [
      '.ai-badge{position:absolute;top:8px;right:8px;display:flex;align-items:center;gap:4px;',
      'padding:2px 7px;border-radius:999px;font:600 11px/1.5 var(--mono,ui-monospace,monospace);',
      'background:rgba(8,10,12,.72);border:1px solid rgba(255,255,255,.14);color:#e8ecef;',
      'backdrop-filter:blur(6px);pointer-events:none;white-space:nowrap}',
      '.ai-badge.is-weak{color:#f0b46a;border-color:rgba(240,180,106,.45)}',
      '.ai-badge.is-low{color:#f08b7c;border-color:rgba(240,139,124,.45)}',
      '.ai-badge small{font-weight:400;color:#9aa3ab;font-size:10px}',
      '.ai-tags{display:flex;flex-wrap:wrap;gap:4px;margin-top:4px}',
      '.ai-tag{font:500 10px/1.5 var(--sans,system-ui,sans-serif);padding:1px 6px;border-radius:4px;',
      'background:rgba(120,170,255,.12);border:1px solid rgba(120,170,255,.28);color:#a9c8ff}',
      '.ai-viewer{border:1px solid rgba(255,255,255,.08);border-radius:10px;padding:10px 12px;margin:10px 0}',
      '.ai-viewer-head{display:flex;align-items:baseline;justify-content:space-between;gap:8px;margin-bottom:6px}',
      '.ai-viewer-score{font:600 20px/1.2 var(--mono,ui-monospace,monospace);color:#e8ecef}',
      '.ai-viewer-hint{font-size:11px;color:var(--text-faint,#8a9199)}',
      '.ai-bars{display:grid;gap:5px;margin:6px 0 2px}',
      '.ai-bar{display:grid;grid-template-columns:52px 1fr 34px;align-items:center;gap:8px;font-size:11px;color:var(--text-faint,#8a9199)}',
      '.ai-bar i{display:block;height:4px;border-radius:2px;background:rgba(255,255,255,.1);overflow:hidden}',
      '.ai-bar i b{display:block;height:100%;background:linear-gradient(90deg,#5a8dd6,#8fb7ef)}',
      '.ai-bar span{text-align:right;font-family:var(--mono,ui-monospace,monospace)}',
      '.ai-note{margin:6px 0 0;font-size:11px;line-height:1.6;color:var(--text-faint,#8a9199)}',
      '.ai-panel-row{display:flex;gap:6px;flex-wrap:wrap;margin-top:8px}',
      '.ai-panel-row .btn{flex:1 1 auto}',
      '.ai-panel-stat{font:600 12px/1.6 var(--mono,ui-monospace,monospace);color:#e8ecef}'
    ].join('');
    document.head.appendChild(style);
  }

  function statusText(done, total) {
    if (!total) return '尚未分析';
    return done + ' / ' + total + ' 张已增强';
  }

  function chipsFor(tags) {
    var row = document.createElement('div');
    row.className = 'ai-tags';
    (tags || []).forEach(function (tag) {
      var chip = document.createElement('span');
      chip.className = 'ai-tag';
      chip.textContent = tag.zh || tag.key;
      chip.title = (tag.zh || tag.key) + ' · ' + Math.round((tag.p || 0) * 100) + '%';
      row.appendChild(chip);
    });
    return row;
  }

  function decorate(info, media, photo) {
    if (!photo || !photo.ai) return;
    injectStyles();
    var ai = photo.ai;
    var composite = Math.round(ai.composite || 0);
    var badge = document.createElement('span');
    badge.className = 'ai-badge' + (composite < 50 ? ' is-low' : (composite < 68 ? ' is-weak' : ''));
    badge.title = '综合分 ' + composite + ' · 对焦 ' + Math.round(ai.parts.focus) +
      ' · 运动模糊风险 ' + Math.round((ai.motionRisk || 0) * 100) + '%';
    badge.appendChild(document.createTextNode('AI ' + composite));
    if (ai.star === 0) {
      var low = document.createElement('small');
      low.textContent = '建议废片';
      badge.appendChild(low);
    }
    if (media) media.appendChild(badge);
    if (info && ai.tags && ai.tags.length) info.appendChild(chipsFor(ai.tags));
  }

  function bar(label, value) {
    var row = document.createElement('div');
    row.className = 'ai-bar';
    var name = document.createElement('span');
    name.textContent = label;
    var track = document.createElement('i');
    var fill = document.createElement('b');
    fill.style.width = Math.max(0, Math.min(100, value)) + '%';
    track.appendChild(fill);
    var num = document.createElement('span');
    num.textContent = Math.round(value);
    row.appendChild(name);
    row.appendChild(track);
    row.appendChild(num);
    return row;
  }

  function renderViewer(mount, photo) {
    if (!mount) return;
    var ai = photo && photo.ai;
    if (!ai) {
      mount.hidden = true;
      mount.replaceChildren();
      return;
    }
    injectStyles();
    mount.hidden = false;
    mount.replaceChildren();

    var head = document.createElement('div');
    head.className = 'ai-viewer-head';
    var score = document.createElement('span');
    score.className = 'ai-viewer-score';
    score.textContent = String(Math.round(ai.composite));
    var hint = document.createElement('span');
    hint.className = 'ai-viewer-hint';
    var verdictText = ai.verdict === 'sharp' ? '合焦' : (ai.verdict === 'border' ? '临界' : '失焦');
    hint.textContent = '综合分 · ' + verdictText + ' · 建议 ' + (ai.star || 0) + ' 星';
    head.appendChild(score);
    head.appendChild(hint);
    mount.appendChild(head);

    var bars = document.createElement('div');
    bars.className = 'ai-bars';
    bars.appendChild(bar('对焦', ai.parts.focus));
    bars.appendChild(bar('稳定性', ai.parts.motion));
    bars.appendChild(bar('曝光', ai.parts.exposure));
    bars.appendChild(bar('噪声', ai.parts.noise));
    bars.appendChild(bar('主体', ai.parts.subject));
    mount.appendChild(bars);

    if (ai.tags && ai.tags.length) mount.appendChild(chipsFor(ai.tags));

    var note = document.createElement('p');
    note.className = 'ai-note';
    var bits = [];
    bits.push('运动模糊风险 ' + Math.round((ai.motionRisk || 0) * 100) + '%');
    bits.push('纹理覆盖 ' + Math.round((ai.textureRatio || 0) * 100) + '%');
    if (ai.exif && ai.exif.ISO) bits.push('ISO ' + Math.round(ai.exif.ISO));
    if (ai.cached) bits.push('来自本机缓存');
    note.textContent = bits.join(' · ');
    mount.appendChild(note);
  }

  async function request(path, body) {
    var response = await fetch(endpoint(path), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body)
    });
    return response.json();
  }

  function collect(photos) {
    return photos.filter(function (photo) {
      return photo && photo.status === 'done' && pathOf(photo);
    });
  }

  async function run(photos, options) {
    options = options || {};
    if (!available()) return { ok: false, error: 'AI 增强需要桌面版' };
    var list = collect(photos);
    if (!list.length) return { ok: false, error: '没有可分析的照片' };

    injectStyles();
    var byId = {};
    list.forEach(function (photo) { byId[photo.id] = photo; });

    var tagState = { available: true, reason: '' };
    var failed = 0;
    var updated = 0;
    for (var start = 0; start < list.length; start += BATCH) {
      var slice = list.slice(start, start + BATCH);
      var payload = {
        items: slice.map(function (photo) {
          return { id: photo.id, path: pathOf(photo) };
        }),
        tags: options.tags !== false,
        force: !!options.force
      };
      var result;
      try {
        result = await request('/api/ai/analyze', payload);
      } catch (error) {
        return { ok: false, error: '本地分析服务没有响应', done: updated };
      }
      if (!result || result.ok === false) {
        return { ok: false, error: (result && (result.error || result.reason)) || '分析失败', done: updated };
      }
      if (result.tags) tagState = result.tags;
      (result.results || []).forEach(function (item) {
        var photo = byId[item.id];
        if (!photo) return;
        if (!item.ok) {
          failed += 1;
          photo.aiError = item.error || '分析失败';
          return;
        }
        photo.ai = {
          composite: item.score.composite,
          parts: item.score.parts,
          verdict: item.score.verdict,
          star: item.score.star,
          metrics: item.metrics,
          motionRisk: item.metrics.motion_risk,
          textureRatio: item.metrics.texture_ratio,
          tags: item.tags || [],
          exif: item.exif || {},
          cached: !!item.cached
        };
        photo.cacheDirty = true;
        updated += 1;
      });
      if (options.onProgress) options.onProgress(Math.min(list.length, start + slice.length), list.length);
    }
    return {
      ok: true,
      done: updated,
      failed: failed,
      tags: tagState,
      total: list.length
    };
  }

  /* 按 AI 建议打星：手动改过的照片不覆盖，组内最佳补一星。 */
  function applySuggested(photos, groups, byId) {
    var bestOfGroup = {};
    (groups || []).forEach(function (ids) {
      var best = null;
      ids.forEach(function (id) {
        var photo = byId(id);
        if (!photo || !photo.ai) return;
        if (!best || photo.ai.composite > best.ai.composite) best = photo;
      });
      if (best) bestOfGroup[best.id] = true;
    });
    var changed = 0;
    var rejected = 0;
    photos.forEach(function (photo) {
      if (!photo.ai || photo.starTouched) return;
      var star = photo.ai.star || 0;
      if (star > 0 && bestOfGroup[photo.id]) star = Math.min(5, star + 1);
      if (star === 0) rejected += 1;
      if (photo.rating !== star) changed += 1;
      photo.rating = star;
      photo.starAuto = star > 0;
      photo.keep = star >= 4;
    });
    return { changed: changed, rejected: rejected };
  }

  root.AIEnhance = {
    available: available,
    run: run,
    decorate: decorate,
    renderViewer: renderViewer,
    applySuggested: applySuggested,
    pathOf: pathOf,
    statusText: statusText,
    injectStyles: injectStyles
  };
})(typeof self !== 'undefined' ? self : this);
