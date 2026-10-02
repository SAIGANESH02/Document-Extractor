const $ = s => document.querySelector(s);
const esc = s => String(s).replace(/[<>&"]/g,
  c => ({'<':'&lt;','>':'&gt;','&':'&amp;','"':'&quot;'}[c]));
const cv = $('#cv'), ctx = cv.getContext('2d');
let img = null, regions = [], tiles = [], suppress = [], page = null, sel = -1;
let observations = [], answers = {}, provenance = {}, scoreData = null;
let hlBox = null, selAns = null;
let stageData = {}, catalog = {}, reviewQ = [], parallel = [], lastSummary = null, tablesData = [];
let view = {x:0, y:0, k:1}, nat = {w:1, h:1}, dragging = false, last = null, moved = 0;

const STAGE_HTML = s => `
  <div class="stage" data-s="${s.status}" id="st-${s.id}"${
    s.data?.errors?.length ? ` title="${esc(s.data.errors.join(' | '))}"` : ''}>
    <div class="id">LAYER ${s.id}</div><div class="nm">${s.name}</div>
    <div class="dt">${s.detail}</div>
    <div class="note">${esc(s.note || (s.status==='waiting'?'':s.status))}${s.seconds!=null?` · ${s.seconds}s`:''}</div>
    ${s.data?.started_at!=null ? `<div class="tm">started +${s.data.started_at}s${
      s.data.finished_at!=null ? ` · finished +${s.data.finished_at}s` : ''}</div>` : ''}
    <div class="bar" style="width:${(s.data?.progress||0)*100}%"></div>
  </div>`;

// C, D and T run side by side; they are drawn in one dashed group so it is
// visible that they started together and progress independently.
function renderRail(stages){
  const par = stages.filter(s => parallel.includes(s.id));
  const start = par.find(s => s.data?.started_at!=null)?.data.started_at;
  let html = '', done = false;
  for (const s of stages){
    if (!parallel.includes(s.id)){ html += STAGE_HTML(s); continue; }
    if (done) continue;
    done = true;
    html += `<div class="par"><div class="pl" id="parLabel">RUN IN PARALLEL · ${par.map(x=>x.id).join(' ∥ ')}${
      start!=null ? ` · all started at +${start}s` : ''}</div>
      <div class="pr">${par.map(STAGE_HTML).join('')}</div></div>`;
  }
  $('#rail').innerHTML = html;
  wireRail();
}

function runConfig(){
  const q = new URLSearchParams({
    model_b: $('#mB').value || '', d_mode: $('#mMode').value || 'single',
    model_d: $('#mSingle').value || '', model_t: $('#mT').value || '',
    light_a: $('#mLightA').value || '', light_b: $('#mLightB').value || '',
    flagship: $('#mFlag').value || ''});
  return q.toString();
}

/* ---------- data ---------- */
async function loadDocs(select){
  const docs = await (await fetch('/api/documents')).json();
  $('#doc').innerHTML = docs.map(d =>
    `<option value="${d.page}">Page ${d.page}${d.source_pdf?` (${esc(d.source_pdf)})`:''} — ${d.width}×${d.height}${d.cached?' · cached':''}</option>`).join('');
  if (select) $('#doc').value = select;
}

$('#upload').onchange = async e => {
  const f = e.target.files[0]; if (!f) return;
  const fd = new FormData(); fd.append('file', f);
  const r = await (await fetch('/api/upload', {method:'POST', body:fd})).json();
  e.target.value = '';
  if (r.error) { alert(r.error); return; }
  await loadDocs(r.pages[0].page);
  $('#doc').dispatchEvent(new Event('change'));
};

function runPipeline(force, cacheOnly){
  page = +$('#doc').value;
  regions = []; tiles = []; suppress = []; sel = -1;
  observations = []; answers = {}; provenance = {}; scoreData = null; hlBox = null; selAns = null;
  renderList(); renderAnswers(); $('#insp').innerHTML = '<span class="hint">Click a box or an answer to see the full-resolution pixels it came from.</span>';
  $('#run').disabled = $('#rerun').disabled = true;
  $('#empty').style.display = 'none';

  tablesData = []; renderTables(); renderStats(null);
  if (window.__es) window.__es.close();          // switching pages ends the previous stream
  const es = window.__es = new EventSource(`/api/run?page=${page}&force=${force?1:0}&cache_only=${cacheOnly?1:0}&${runConfig()}`);
  es.onmessage = e => {
    const ev = JSON.parse(e.data);
    if (ev.type === 'init'){
      stageData = {}; parallel = ev.parallel || [];
      ev.stages.forEach(s => stageData[s.id] = s);
      renderRail(ev.stages);
    }
    if (ev.type === 'stats') renderStats(ev.stats);
    if (ev.type === 'stage'){
      stageData[ev.stage.id] = ev.stage;
      const el = $('#st-'+ev.stage.id);
      // outerHTML replaces the node, so any handler bound to it dies with it.
      // Re-bind after every stage update, not just once at init.
      if (parallel.includes(ev.stage.id)) renderRail(Object.values(stageData));
      else if (el){ el.outerHTML = STAGE_HTML(ev.stage); wireRail(); }
      if (ev.stage.id === 'A' && ev.stage.data?.tiles){ tiles = ev.stage.data.tiles; loadImage(); }
      if (ev.stage.id === 'B' && ev.stage.data?.context){
        suppress = ev.stage.data.context.suppress_regions || []; draw(); }
      if (ev.stage.id === 'C' && ev.stage.data?.regions){ regions = ev.stage.data.regions; renderList(); draw(); }
      if (ev.stage.id === 'D' && ev.stage.data?.observations){
        observations = ev.stage.data.observations; draw(); renderReaders();
        if (sel >= 0) select(sel);                       // fill in the Layer D read
      }
      if (ev.stage.id === 'T' && ev.stage.data?.tables){ tablesData = ev.stage.data.tables; renderTables(); }
      if (ev.stage.id === 'E' && ev.stage.data?.answers){
        answers = ev.stage.data.answers; provenance = ev.stage.data.provenance || {};
        renderAnswers();
      }
    }
    if (ev.type === 'done'){ es.close(); finish(ev.summary); }
    if (ev.type === 'error'){ es.close(); alert('Pipeline error: '+ev.message);
      $('#run').disabled = $('#rerun').disabled = false; }
  };
  es.onerror = () => { es.close(); $('#run').disabled = $('#rerun').disabled = false; };
}

function finish(s){
  $('#run').disabled = $('#rerun').disabled = false;
  lastSummary = s;
  if (s.tables){ tablesData = s.tables; renderTables(); }
  if (s.stats) renderStats(s.stats);
  renderReadme(s);
  loadRules(true);
  if (s.suppress_regions) suppress = s.suppress_regions;
  if (s.answers) answers = s.answers;
  if (s.provenance) provenance = s.provenance;
  scoreData = s.score || null;
  const t = scoreData?.totals || {};
  const pct = v => v == null ? '—' : (v*100).toFixed(0)+'%';
  $('#stats').innerHTML = `
    ${s.sheet_id ? `<div style="flex-basis:100%"><b style="font-size:13px">${s.sheet_id}</b>
       <span>${s.sheet_type||''}${s.document_id!=null?` · doc ${s.document_id}`:' · not in the answer key'}
       · read from the sheet's own title block</span></div>` : ''}
    <div><b>${s.regions}</b><span>regions</span></div>
    <div><b>${s.observations ?? 0}</b><span>observations</span></div>
    <div><b>${s.tiles}</b><span>tiles</span></div>
    <div><b>${pct(s.coverage)}</b><span>coverage</span></div>
    ${scoreData?.has_key ? `<div><b style="color:var(--ok)">${pct(t.recall)}</b><span>recall</span></div>
       <div><b>${pct(t.key_precision)}</b><span>key prec.</span></div>` : ''}`;
  $('#stats').insertAdjacentHTML('beforeend',
    `<div class="hint" style="flex-basis:100%">${s.coverage == null
        ? 'Coverage ' + s.coverage_note : s.coverage_note}${
      scoreData?.has_key ? ` · ${t.matched} matched, ${t.missed} missed, ${t.extra} not in key
        (adjudication queue, not errors)` : ''}</div>`);
  renderAnswers();
  draw();
}

function loadImage(){
  img = new Image();
  img.onload = () => { fit(); draw(); };
  img.src = `/api/page/${page}.png`;
}

/* ---------- view ---------- */
function resize(){
  const r = cv.parentElement.getBoundingClientRect(), d = devicePixelRatio || 1;
  cv.width = r.width*d; cv.height = r.height*d; ctx.setTransform(d,0,0,d,0,0);
  draw();
}
function fit(){
  if (!img) return;
  const r = cv.parentElement.getBoundingClientRect();
  view.k = Math.min(r.width/img.width, r.height/img.height) * 0.96;
  view.x = (r.width - img.width*view.k)/2;
  view.y = (r.height - img.height*view.k)/2;
  draw();
}
// regions are in NATIVE page coords; the display raster is scaled.
const sc = () => img ? img.width / nat.w : 1;
const visible = () => {
  const min = +$('#fConf').value/100, small = $('#fSmall').checked;
  return regions.filter(r => r.score >= min && (!small || (r.bbox[3]-r.bbox[1]) < 40));
};

function draw(){
  const r = cv.parentElement.getBoundingClientRect();
  ctx.clearRect(0,0,r.width,r.height);
  if (!img) return;
  ctx.save(); ctx.translate(view.x, view.y); ctx.scale(view.k, view.k);
  ctx.imageSmoothingQuality = 'high';
  ctx.drawImage(img, 0, 0);
  const s = sc();

  if ($('#fTiles').checked){
    ctx.strokeStyle = 'rgba(91,157,255,.5)'; ctx.lineWidth = 1.5/view.k;
    for (const t of tiles) ctx.strokeRect(t[0]*s, t[1]*s, (t[2]-t[0])*s, (t[3]-t[1])*s);
  }
  if ($('#fSupp').checked){
    for (const z of suppress){
      const [x0,y0,x1,y1] = z.bbox;
      ctx.fillStyle = 'rgba(255,180,84,.16)';
      ctx.strokeStyle = 'rgba(255,180,84,.85)';
      ctx.lineWidth = 2/view.k;
      ctx.fillRect(x0*s, y0*s, (x1-x0)*s, (y1-y0)*s);
      ctx.strokeRect(x0*s, y0*s, (x1-x0)*s, (y1-y0)*s);
      ctx.fillStyle = 'rgba(190,110,0,.95)';
      ctx.font = `${Math.max(11, 15/view.k)}px ui-sans-serif`;
      ctx.fillText(z.kind, x0*s + 6/view.k, y0*s + 20/view.k);
    }
  }
  if ($('#fObs').checked){
    ctx.lineWidth = Math.max(0.8, 1.4/view.k);
    // Colour by who settled the reading when the cascade produced it.
    const SRC = {agreed:'rgba(61,220,151,.95)', adjudicated:'rgba(170,110,255,.95)', unsettled:'rgba(255,170,60,.95)'};
    for (const o of observations){
      const [x0,y0,x1,y1] = o.bbox;
      ctx.strokeStyle = SRC[o.source] || 'rgba(91,157,255,.9)';
      ctx.strokeRect(x0*s, y0*s, (x1-x0)*s, (y1-y0)*s);
    }
  }
  const vis = visible();
  ctx.lineWidth = Math.max(0.8, 1.6/view.k);
  for (const rg of vis){
    const [x0,y0,x1,y1] = rg.bbox, h = y1-y0;
    ctx.strokeStyle = h < 40 ? 'rgba(255,60,60,.92)' : 'rgba(255,150,40,.85)';
    ctx.strokeRect(x0*s, y0*s, (x1-x0)*s, h*s);
  }
  const hl = hlBox || (sel >= 0 && regions[sel] ? regions[sel].bbox : null);
  if (hl){
    const [x0,y0,x1,y1] = hl;
    ctx.strokeStyle = '#3ddc97'; ctx.lineWidth = Math.max(2, 3.5/view.k);
    ctx.strokeRect(x0*s-3, y0*s-3, (x1-x0)*s+6, (y1-y0)*s+6);
  }
  ctx.restore();
}

/* ---------- interaction ---------- */
cv.addEventListener('wheel', e => {
  e.preventDefault();
  const r = cv.getBoundingClientRect(), mx = e.clientX-r.left, my = e.clientY-r.top;
  const f = Math.exp(-e.deltaY * 0.0016), k = Math.min(40, Math.max(0.05, view.k*f));
  view.x = mx - (mx-view.x)*(k/view.k); view.y = my - (my-view.y)*(k/view.k); view.k = k;
  draw();
}, {passive:false});

cv.addEventListener('pointerdown', e => {
  dragging = true; moved = 0; last = [e.clientX, e.clientY];
  cv.classList.add('drag'); cv.setPointerCapture(e.pointerId);
});
cv.addEventListener('pointermove', e => {
  if (!dragging) return;
  const dx = e.clientX-last[0], dy = e.clientY-last[1];
  moved += Math.abs(dx)+Math.abs(dy);
  view.x += dx; view.y += dy; last = [e.clientX, e.clientY]; draw();
});
cv.addEventListener('pointerup', e => {
  dragging = false; cv.classList.remove('drag');
  if (moved > 4 || !img) return;                       // a drag, not a click
  const r = cv.getBoundingClientRect(), s = sc();
  const px = ((e.clientX-r.left)-view.x)/view.k/s, py = ((e.clientY-r.top)-view.y)/view.k/s;
  const pad = 6/view.k/s;
  const hits = visible().filter(g =>
    px >= g.bbox[0]-pad && px <= g.bbox[2]+pad && py >= g.bbox[1]-pad && py <= g.bbox[3]+pad);
  if (!hits.length) return;
  hits.sort((a,b) => (a.bbox[2]-a.bbox[0])*(a.bbox[3]-a.bbox[1]) - (b.bbox[2]-b.bbox[0])*(b.bbox[3]-b.bbox[1]));
  select(regions.indexOf(hits[0]));
});

/* ---------- inspector ---------- */
const iou = (a,b) => {
  const ix0=Math.max(a[0],b[0]), iy0=Math.max(a[1],b[1]);
  const ix1=Math.min(a[2],b[2]), iy1=Math.min(a[3],b[3]);
  const inter = Math.max(0,ix1-ix0)*Math.max(0,iy1-iy0);
  if (!inter) return 0;
  const ua = (a[2]-a[0])*(a[3]-a[1]) + (b[2]-b[0])*(b[3]-b[1]) - inter;
  return ua ? inter/ua : 0;
};
// The observation Layer D made at the same place the detector found a region.
const obsFor = bbox => {
  let best = null, bi = 0.25;
  for (const o of observations){ const v = iou(bbox, o.bbox); if (v > bi){ bi = v; best = o; } }
  return best;
};

function select(i){
  sel = i; hlBox = null; selAns = null;
  document.querySelectorAll('.v.sel').forEach(el => el.classList.remove('sel'));
  draw();
  document.querySelectorAll('.row').forEach(el => el.classList.toggle('sel', +el.dataset.i === i));
  const r = regions[i]; if (!r) return;
  const d = obsFor(r.bbox);
  const [x0,y0,x1,y1] = r.bbox;
  const q = `page=${page}&x0=${x0}&y0=${y0}&x1=${x1}&y1=${y1}`;
  $('#insp').innerHTML = `
    <img id="crop" src="/api/crop?${q}" alt="native-resolution crop">
    <dl class="kv">
      <dt>Layer C read</dt><dd class="read">${r.text ? esc(r.text) : '<span style="color:var(--dim)">detection only</span>'}</dd>
      <dt>Layer D read</dt><dd class="read">${d
        ? esc(d.text || '(no text)') + `<span style="color:var(--dim)"> · ${d.confidence?.toFixed(2)}</span>`
        : `<span style="color:var(--dim)">${observations.length ? 'no observation here' : '—'}</span>`}</dd>
      ${d ? `<dt>kind</dt><dd>${esc(d.kind||'')}</dd>
      <dt>symbol</dt><dd style="font-size:11.5px">${esc(d.symbol||'')}</dd>
      <dt>attached to</dt><dd style="font-size:11.5px">${esc(d.attached_to||'')}</dd>` : ''}
      <dt>det score</dt><dd>${r.score.toFixed(3)}</dd>
      ${r.text_score!=null?`<dt>read conf</dt><dd>${r.text_score.toFixed(3)}</dd>`:''}
      <dt>size</dt><dd>${x1-x0} × ${y1-y0} px</dd>
      <dt>page xy</dt><dd>${x0}, ${y0}</dd>
      <dt>tile</dt><dd>${r.tile_id ?? '—'}</dd>
    </dl>
    <div class="hint" style="margin-top:8px">Crop is from the 5088px original, not the display raster.</div>`;
  const el = document.querySelector(`.row[data-i="${i}"]`);
  if (el) el.scrollIntoView({block:'nearest'});
}

/* ---------- answers ---------- */
function renderAnswers(){
  const cats = Object.keys(answers);
  const sc = scoreData?.categories || {};
  // A key category we found nothing for still has to show up — a silent
  // missing category is the failure this panel exists to make visible.
  for (const c of Object.keys(sc)) if (!cats.includes(c)) cats.push(c);
  $('#ansHdr').textContent = `Answers — ${cats.length} categories`;
  if (!cats.length){
    $('#answers').innerHTML = '<span class="hint" style="padding:8px;display:block">Layer E output appears here.</span>';
    return;
  }
  const normed = v => String(v).toUpperCase().replace(/[^A-Z0-9]/g,'');
  $('#answers').innerHTML = cats.sort().map(cat => {
    const raw = answers[cat];
    const got = raw == null ? [] : (Array.isArray(raw) ? raw : [raw]);
    const cs = sc[cat];
    const expected = cs?.expected || [];
    const foundSet = new Set((cs?.found || []).map(normed));
    const chips = got.map((v,i) => {
      const cls = !cs || !expected.length ? '' : (foundSet.has(normed(v)) ? ' found' : ' extra');
      return `<span class="v${cls}" data-cat="${esc(cat)}" data-i="${i}"
        title="${cls.trim()==='extra'?'not in the answer key — adjudicate':''}">${esc(String(v))}</span>`;
    });
    // key values we never emitted
    const missed = (cs?.missed || []).map(v =>
      `<span class="v missed" title="in the key, not found by the pipeline">${esc(String(v))}</span>`);
    const rec = cs?.recall;
    return `<div class="cat">
      <div class="ch"><span>${esc(cat)}</span>
        <span class="r">${rec==null?'':(rec*100).toFixed(0)+'%'}</span></div>
      <div class="vals">${chips.join('')}${missed.join('')}
        ${!chips.length && !missed.length ? '<span class="hint">—</span>' : ''}</div>
    </div>`;
  }).join('');
  $('#answers').querySelectorAll('.v:not(.missed)').forEach(el =>
    el.onclick = () => selectAnswer(el.dataset.cat, +el.dataset.i, el));
}

function selectAnswer(cat, i, el){
  const p = (provenance[cat] || [])[i];
  document.querySelectorAll('.v.sel').forEach(e => e.classList.remove('sel'));
  if (el) el.classList.add('sel');
  if (!p){
    $('#insp').innerHTML = `<span class="hint">No provenance recorded for this value
      (it came from sheet context, not from an observation).</span>`;
    hlBox = null; sel = -1; draw(); return;
  }
  sel = -1; selAns = [cat, i]; hlBox = p.bbox;
  document.querySelectorAll('.row').forEach(e => e.classList.remove('sel'));
  const [x0,y0,x1,y1] = p.bbox;
  const q = `page=${page}&x0=${x0}&y0=${y0}&x1=${x1}&y1=${y1}`;
  $('#insp').innerHTML = `
    <img id="crop" src="/api/crop?${q}" alt="native-resolution crop">
    <dl class="kv">
      <dt>value</dt><dd class="read">${esc(String(p.value))}</dd>
      <dt>category</dt><dd style="font-size:11.5px">${esc(cat)}</dd>
      <dt>Layer D read</dt><dd class="read">${esc(p.text || '(no text)')}</dd>
      <dt>symbol</dt><dd style="font-size:11.5px">${esc(p.symbol || '')}</dd>
      <dt>read conf</dt><dd>${p.confidence?.toFixed(2) ?? '—'}</dd>
      <dt>rule</dt><dd>RULES.yaml #${p.rule}</dd>
      ${p.flag_letter ? `<dt>flag letter</dt><dd><b>${esc(p.flag_letter.letter)}</b> · ${esc(p.flag_letter.evidence||'')}
        <span class="hint">(${esc(p.flag_letter.model||'')})</span></dd>` : ''}
      ${p.visual_check ? `<dt>visual check</dt><dd><b>${esc(p.visual_check.answer)}</b> · ${esc(p.visual_check.evidence||'')}
        <span class="hint">(${esc(p.visual_check.model||'')})</span></dd>` : ''}
      <dt>page xy</dt><dd>${x0}, ${y0}</dd>
      <dt>tile</dt><dd>${p.tile_id ?? '—'}</dd>
    </dl>
    <div class="hint" style="margin-top:8px">Every emitted value traces back to one
      observation, one bbox and one rule.</div>`;
  panTo(p.bbox);
  draw();
}

// Bring a bbox into view without changing zoom — answers can sit anywhere.
function panTo(bbox){
  if (!img) return;
  const r = cv.parentElement.getBoundingClientRect(), s = sc();
  const cx = (bbox[0]+bbox[2])/2*s, cy = (bbox[1]+bbox[3])/2*s;
  const px = view.x + cx*view.k, py = view.y + cy*view.k;
  if (px < 0 || py < 0 || px > r.width || py > r.height){
    view.x = r.width/2 - cx*view.k;
    view.y = r.height/2 - cy*view.k;
  }
}

function renderList(){
  const vis = visible();
  $('#listHdr').textContent = `Regions — ${vis.length}${vis.length!==regions.length?` of ${regions.length}`:''}`;
  $('#list').innerHTML = vis.map(r => {
    const i = regions.indexOf(r);
    return `<div class="row${i===sel?' sel':''}" data-i="${i}">
      <span class="t">${r.text ? esc(r.text) : `· ${r.bbox[2]-r.bbox[0]}×${r.bbox[3]-r.bbox[1]}`}</span>
      <span class="c">${r.score.toFixed(2)}</span></div>`;
  }).join('') || '<div class="hint" style="padding:8px">No regions match the filters.</div>';
  $('#list').querySelectorAll('.row').forEach(el =>
    el.onclick = () => select(+el.dataset.i));
}

/* ---------- tabs ---------- */
document.querySelectorAll('.tab').forEach(t => t.onclick = () => {
  document.querySelectorAll('.tab').forEach(x => x.classList.toggle('on', x === t));
  document.querySelectorAll('.pane').forEach(p =>
    p.classList.toggle('on', p.id === 'p-' + t.dataset.p));
  if (t.dataset.p === 'review' && !reviewQ.length) loadReview();
  if (t.dataset.p === 'rules') loadRules(false);
});

/* ---------- per-layer detail ---------- */
// The rail is re-rendered on every stage event, so handlers are re-bound
// each time rather than attached once.
function wireRail(){
  document.querySelectorAll('.stage').forEach(el =>
    el.onclick = () => showLayer(el.id.replace('st-','')));
}

const LAYER_IO = {
  A: {in:'The page raster at native resolution', out:'Tile geometry used by every later stage',
      does:'Opens the 5088x3296 page and cuts a 15-tile grid with 15% overlap, so a tag sitting on a seam appears in two tiles and cannot fall between them.'},
  B: {in:'The whole page, downscaled to 2200px', out:'Sheet id, symbol dictionary, note/legend boxes',
      does:'One vision call reads the drawing\'s OWN legend. This is what lets an unseen sheet be handled without retuning, and it tells Layer E which areas are prose whose tag-shaped text must be ignored.'},
  C: {in:'Each tile at native resolution', out:'Text-region boxes, with no text',
      does:'Local PaddleOCR on CPU: no API, no network. It only finds where text is, never reads it, so it cannot invent anything. Runs in parallel with D; afterwards its boxes show where D skipped text (coverage, and unread items in Review).'},
  D: {in:'Each 1250px tile plus the Layer B context', out:'Observations: text, symbol, what it attaches to, bbox',
      does:'The reader. Reports only what it SEES and is forbidden from naming answer-key categories, so adding a category never means re-running this expensive stage.'},
  T: {in:'The whole page (to find tables), then each table cropped at full resolution', out:'Every table, cell by cell (CSV + Markdown)',
      does:'Runs on any sheet: one call lists the data tables (none → done), then one call per table reads every cell exactly. Runs in parallel with C and D because it needs nothing from them.'},
  V: {in:'Each candidate symbol, cropped at full resolution and outlined in red', out:'yes / no / unclear + the visible evidence, per tag',
      does:'For categories defined by a drawn shape (a PCV whose stem ends on a U-bend), the model is shown the symbol itself and asked one yes/no question, instead of matching words in its earlier description. Answers are cached; without one, the rule falls back to its wording test.'},
  E: {in:'Observations, sheet context and RULES.yaml', out:'Answer-key shaped values with provenance',
      does:'Deterministic: no model, no network, about 0.15s. Drops low confidence, suppresses note/legend regions, de-duplicates tile seams, then maps observations to categories by rule. Every value traces back to one observation and one rule.'},
};

function showLayer(id){
  document.querySelectorAll('.stage').forEach(e => e.classList.toggle('active', e.id === 'st-'+id));
  document.querySelectorAll('.tab').forEach(x => x.classList.toggle('on', x.dataset.p === 'layer'));
  document.querySelectorAll('.pane').forEach(p => p.classList.toggle('on', p.id === 'p-layer'));
  const s = stageData[id], io = LAYER_IO[id] || {};
  if (!s){ $('#layerDetail').innerHTML = '<span class="hint">No data for this layer yet.</span>'; return; }
  const d = s.data || {};
  const rows = [];
  const push = (k,v) => { if (v!=null && v!=='') rows.push(`<dt>${esc(k)}</dt><dd>${v}</dd>`); };
  push('status', `<b style="color:var(--${s.status==='done'?'ok':s.status==='error'?'bad':'dim'})">${esc(s.status)}</b>`);
  push('time', s.seconds!=null ? s.seconds+'s' : null);
  push('note', esc(s.note||''));
  if (d.regions) push('regions out', d.regions.length);
  if (d.observations) push('observations out', d.observations.length);
  if (d.tiles) push('tiles', d.tiles.length);
  if (d.context) push('sheet id', esc(d.context.sheet_id||''));
  if (d.usage) push('tokens', `in ${d.usage.input_tokens??'-'} / out ${d.usage.output_tokens??'-'}`);
  if (d.meta && d.meta.model) push('model', esc(d.meta.model));
  if (d.audit) push('kept', `${d.audit.resolved} of ${d.audit.input}`);
  if (d.cached != null) push('source', d.cached
    ? `cache${d.cached_at?` · saved ${esc(d.cached_at)}`:''}${d.cache_file?` · <code>${esc(d.cache_file)}</code>`:''}`
    : 'fresh run');

  let extra = '';
  if (d.errors && d.errors.length)
    extra += `<h3>Errors</h3><pre style="color:var(--bad)">${esc(d.errors.join('\n'))}</pre>`;
  if (d.audit)
    extra += `<h3>Layer E funnel</h3><pre>${esc(JSON.stringify(d.audit,null,1))}</pre>`;
  if (d.cascade)
    extra += `<h3>Cascade</h3><pre>${esc(JSON.stringify(d.cascade,null,1))}</pre>`;

  $('#layerDetail').innerHTML = `
    <div style="font-weight:650;font-size:13px;margin-bottom:2px">Layer ${esc(id)} &middot; ${esc(s.name||'')}</div>
    <div class="hint" style="margin-bottom:9px">${esc(io.does||s.detail||'')}</div>
    <h3>Input</h3><div class="hint">${esc(io.in||'-')}</div>
    <h3>Output</h3><div class="hint">${esc(io.out||'-')}</div>
    <h3>This run</h3><dl>${rows.join('')}</dl>${extra}`;
}

/* ---------- models ---------- */
async function loadModels(){
  const m = await (await fetch('/api/models')).json();
  catalog = m.catalog;
  const opts = tier => Object.entries(catalog).filter(([,v]) => !tier || v.tier===tier)
    .map(([k,v]) => `<option value="${esc(k)}">${esc(k)} &middot; ${esc(v.provider)}</option>`).join('');
  $('#mSingle').innerHTML = $('#mB').innerHTML = $('#mT').innerHTML = opts(null);
  $('#mB').value = m.default_b; $('#mT').value = m.default_t || m.default_flagship;
  $('#mLightA').innerHTML = $('#mLightB').innerHTML = opts('light');
  $('#mFlag').innerHTML = opts('flagship');
  $('#mSingle').value = m.default_d || m.default_flagship;
  $('#mLightA').value = m.default_light[0];
  $('#mLightB').value = m.default_light[1];
  $('#mFlag').value = m.default_flagship;
  checkPair(); showNotes();
}
async function checkPair(){
  const a = $('#mLightA').value, b = $('#mLightB').value;
  const r = await (await fetch(`/api/pair-check?a=${encodeURIComponent(a)}&b=${encodeURIComponent(b)}`)).json();
  $('#pairWarn').innerHTML = r.warning ? `<div class="warn">! ${esc(r.warning)}</div>` : '';
}
function showNotes(){
  const ms = [$('#mLightA').value, $('#mLightB').value, $('#mFlag').value];
  $('#mNotes').innerHTML = '<h3>Selected</h3>' + ms.map(k => {
    const c = catalog[k]; if (!c) return '';
    const price = c.in!=null ? `$${c.in}/$${c.out} per Mtok` : 'price not recorded';
    return `<div class="hint" style="margin-bottom:5px"><b>${esc(k)}</b> &middot; ${esc(price)}<br>${esc(c.note||'')}</div>`;
  }).join('');
}

/* ---------- review ---------- */
async function loadReview(){
  const pg = page || $('#doc').value;
  const r = await (await fetch(`/api/review/${pg}`)).json();
  if (r.error){ $('#revStats').innerHTML = `<span class="hint">${esc(r.error)}</span>`; return; }
  reviewQ = r.queue;
  const s = r.summary, by = s.by_kind||{};
  $('#revN').textContent = s.open;
  $('#revStats').innerHTML = `
    <div><b>${s.open}</b><span>open</span></div>
    <div><b>${s.decided}</b><span>decided</span></div>
    ${Object.entries(by).map(([k,v])=>`<div><b>${v}</b><span>${esc(k)}</span></div>`).join('')}
    <div class="hint" style="flex-basis:100%">From the ${esc(r.model||'last')} run.
      Ordered by how much your decision is worth: contested readings first,
      values merely missing from the key last.</div>`;
  renderReview();
}

function renderReview(){
  $('#revList').innerHTML = reviewQ.map((i,ix) => {
    const d = i.decision;
    const claims = (i.claim_a || i.claim_b)
      ? `<div class="claims">
           ${i.claim_a?`<span class="claim">A: ${esc(i.claim_a)}</span>`:''}
           ${i.claim_b?`<span class="claim">B: ${esc(i.claim_b)}</span>`:''}
           ${i.ruled?`<span class="claim" style="border-color:var(--ok)">ruled: ${esc(i.ruled)}</span>`:''}
         </div>` : '';
    const sub = [i.category, i.zone && `in ${i.zone}`, i.tile_id,
                 i.rule!=null && `rule #${i.rule}`,
                 i.det_score!=null && `det ${i.det_score}`,
                 i.kind==='not_in_key' && `key lists ${i.expected_n}`]
                .filter(Boolean).map(esc).join(' \u00b7 ');
    return `<div class="q${d?' done':''}" data-k="${esc(i.kind)}" data-ix="${ix}">
      <div class="qh"><span class="k">${esc(i.kind)}</span>${i.by?esc(i.by):''}</div>
      <code class="v">${esc(i.value || '(nothing read here)')}</code>
      <div class="sub">${sub}</div>
      ${claims}
      ${d ? `<div class="verdict" style="color:var(--${d.action==='reject'?'bad':d.action==='correct'?'warn':'ok'})">
               ${esc(d.action)}${d.corrected?' \u2192 '+esc(d.corrected):''}</div>`
          : `<div class="acts">
               <button class="ok" data-a="accept">Accept</button>
               <button class="no" data-a="reject">Reject</button>
               <input class="fix" placeholder="corrected value">
               <button data-a="correct">Save fix</button>
             </div>`}
    </div>`;
  }).join('') || '<div class="hint" style="padding:12px">Queue is empty.</div>';

  $('#revList').querySelectorAll('.q').forEach(card => {
    const ix = +card.dataset.ix;
    card.querySelectorAll('button[data-a]').forEach(b => b.onclick = ev => {
      ev.stopPropagation();
      const fix = card.querySelector('.fix');
      decide(ix, b.dataset.a, b.dataset.a==='correct' ? (fix && fix.value || '').trim() : null);
    });
    // clicking the card itself shows the pixels the item came from
    card.onclick = e => { if (!e.target.closest('.acts')) showItem(reviewQ[ix]); };
  });
}

function showItem(i){
  if (!i.bbox) return;
  hlBox = i.bbox; sel = -1; draw(); panTo(i.bbox); draw();
}

async function decide(ix, action, corrected){
  const i = reviewQ[ix];
  if (action === 'correct' && !corrected){ alert('Type a corrected value first.'); return; }
  const pg = page || $('#doc').value;
  const r = await (await fetch(`/api/review/${pg}`, {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({id:i.id, action, corrected})
  })).json();
  if (r.error){ alert(r.error); return; }
  i.decision = r.decision;
  renderReview();
  $('#revN').textContent = reviewQ.filter(x => !x.decision).length;
}

/* ---------- wiring ---------- */
$('#revLoad').onclick = loadReview;
$('#revExport').onclick = async () => {
  const pg = page || $('#doc').value;
  const r = await (await fetch(`/api/review/${pg}/export`)).json();
  $('#revList').insertAdjacentHTML('afterbegin',
    `<pre style="margin:10px 12px;padding:9px;background:var(--panel2);border:1px solid var(--line);
      border-radius:7px;font-size:11px;white-space:pre-wrap">${esc(JSON.stringify(r,null,1))}</pre>`);
};
$('#mLightA').onchange = $('#mLightB').onchange = () => { checkPair(); showNotes(); };
$('#mFlag').onchange = $('#mSingle').onchange = showNotes;
// The header checkbox and the Models tab's mode select are one setting.
const syncMode = cascade => {
  $('#useCascade').checked = cascade; $('#mMode').value = cascade ? 'cascade' : 'single';
  $('#rowSingle').style.display = cascade ? 'none' : '';
};
$('#useCascade').onchange = e => syncMode(e.target.checked);
$('#mMode').onchange = e => syncMode(e.target.value === 'cascade');
$('#run').onclick = () => runPipeline(false);
$('#rerun').onclick = () => runPipeline(true);
$('#fit').onclick = fit;
$('#fSmall').onchange = $('#fTiles').onchange = $('#fSupp').onchange =
  $('#fObs').onchange = () => { renderList(); draw(); };
$('#fConf').oninput = e => { $('#fConfV').textContent = (e.target.value/100).toFixed(2); renderList(); draw(); };
$('#doc').onchange = () => {
  const o = $('#doc').selectedOptions[0].textContent.match(/(\d+)×(\d+)/);
  nat = {w:+o[1], h:+o[2]};
};
addEventListener('resize', resize);
window.addEventListener('keydown', e => {
  if (e.key === 'f') fit();
  if ((e.key === 'j' || e.key === 'k') && regions.length){
    const vis = visible(), cur = vis.indexOf(regions[sel]);
    select(regions.indexOf(vis[Math.max(0, Math.min(vis.length-1, cur + (e.key==='j'?1:-1)))]));
  }
});



/* ---------- live AI stats ---------- */
const money = v => v == null ? '—' : '$' + (v < 0.01 && v > 0 ? v.toFixed(4) : v.toFixed(3));
const LAYER_NAME = {B:'B · sheet context', D:'D · observe', T:'T · tables', V:'V · visual checks', R:'R · rule assistant'};
let lastStats = null, showCalls = false;
function renderStats(st){
  lastStats = st;
  if (!st || (!st.total_calls && !Object.keys(st.cached_layers||{}).length)){
    $('#sbBody').innerHTML = '<span class="hint">No model calls yet' + (st ? ' — every AI layer came from cache.' : '.') + '</span>';
    $('#sbTotal').textContent = st?.wall_seconds!=null ? `run took ${st.wall_seconds}s` : '';
    return;
  }
  const rows = Object.entries(st.layers || {}).map(([L, v]) => `<tr>
      <td class="l">${esc(LAYER_NAME[L] || L)}</td><td class="l">${esc((v.models||[]).join(', '))}</td>
      <td>${v.calls}${v.errors?` <span style="color:var(--bad)">(${v.errors} err)</span>`:''}</td>
      <td>${(v.input_tokens/1000).toFixed(1)}k / ${(v.output_tokens/1000).toFixed(1)}k</td>
      <td>${money(v.cost)}${v.unpriced_calls?` <span title="price not recorded for some models">+?</span>`:''}</td>
      <td>${v.avg_latency ?? '—'}s</td><td>${v.avg_ttft!=null ? v.avg_ttft+'s' : '—'}</td></tr>`);
  for (const [L, c] of Object.entries(st.cached_layers || {}))
    rows.push(`<tr><td class="l">${esc(LAYER_NAME[L]||L)}</td><td class="l hint">from cache</td>
      <td>0</td><td>—</td><td>$0 <span class="hint">(${c!=null ? money(c)+' when made' : 'cost then unknown'})</span></td><td>—</td><td>—</td></tr>`);
  const callRows = showCalls ? (st.calls||[]).slice(-40).reverse().map(c => `<tr>
      <td class="l">${esc(c.layer||'?')}${c.tile?' '+esc(c.tile):''}${c.role&&c.role!=='reader'?' · '+esc(c.role):''}</td>
      <td class="l">${esc(c.model)}</td><td>+${c.at}s</td>
      <td>${c.input_tokens!=null?(c.input_tokens/1000).toFixed(1)+'k / '+(c.output_tokens/1000).toFixed(1)+'k':'—'}</td>
      <td>${c.status==='error'?'<span style="color:var(--bad)">error</span>':money(c.cost)}</td>
      <td>${c.latency}s</td><td>${c.ttft!=null?c.ttft+'s':'—'}</td></tr>`).join('') : '';
  $('#sbTotal').textContent = `${st.total_calls} calls · ${money(st.total_cost)}${st.unpriced_calls?' + unpriced':''}${
    st.wall_seconds!=null ? ` · run ${st.wall_seconds}s` : ''}`;
  $('#sbBody').innerHTML = `<table>
    <tr><th>layer</th><th>model</th><th>calls</th><th>tokens in / out</th><th>cost</th><th>avg time</th><th>first token</th></tr>
    ${rows.join('')}
    <tr class="tot"><td class="l">total</td><td></td><td>${st.total_calls}</td><td></td><td>${money(st.total_cost)}</td><td></td><td></td></tr>
    ${callRows ? `<tr><th colspan="7" style="text-align:left;padding-top:6px">latest calls</th></tr>${callRows}` : ''}
  </table>`;
}
$('#sbToggle').onclick = () => { $('#statsbox').classList.toggle('min');
  $('#sbToggle').textContent = $('#statsbox').classList.contains('min') ? '+' : '–'; };
$('#sbCalls').onclick = () => { showCalls = !showCalls; renderStats(lastStats); };

/* ---------- how to read this run ---------- */
function renderReadme(s){
  const sc = s.score || {}, t = sc.totals || {}, cov = sc.coverage || {};
  const pct = v => v == null ? '—' : (v*100).toFixed(1)+'%';
  const items = [];
  if (sc.has_key){
    items.push(`<b>Recall ${pct(t.recall)}</b> — found ${t.matched} of the ${t.matched + t.missed} values the answer key lists. This is the trustworthy number: every key value is really on the drawing. Misses are struck through in red under Answers.`);
    if (t.cleanup_only_matched) items.push(`<b>${t.cleanup_only_matched} of those match only after ignoring punctuation/spaces</b> (e.g. <code>-</code> vs <code>_</code>). A strict exact-match grader would count them as misses.`);
    items.push(`<b>Key precision ${pct(t.key_precision)}</b> is a floor, not a score: ${t.extra} values we found aren't in the key. The key is a sample, so most of these are likely real — they're in the Review tab to confirm.`);
  } else {
    items.push(`<b>No answer key for this sheet</b>, so there is no recall to report. Judge it by the signals below, then spot-check values in the inspector.`);
    items.push(`Same readings + same rules always give the same answers. That makes the output <i>reproducible</i>, not proven correct — correctness comes from the checks below and your review.`);
  }
  if (cov.coverage != null) items.push(`<b>Coverage ${pct(cov.coverage)}</b> — the AI reported text in ${cov.matched} of the ${cov.detector_regions} places the independent detector found text. The rest are listed as <i>unread</i> in Review: likely misses.`);
  if (s.audit?.unclaimed) items.push(`<b>${s.audit.unclaimed} readings matched no rule.</b> Most are pipe labels and notes; if a category is missing, the Rules tab can propose a rule for it.`);
  if ((s.tables||[]).length) items.push(`<b>${s.tables.length} table(s)</b> reproduced in the Tables tab — check cells against the crop.`);
  $('#readme').innerHTML = `<div style="font-weight:650;margin-bottom:2px">How to read this run</div><ul>${items.map(i=>`<li>${i}</li>`).join('')}</ul>`;
}

/* ---------- tables ---------- */
function renderTables(){
  $('#tabN').textContent = tablesData.length || '–';
  if (!tablesData.length){
    $('#tablesList').innerHTML = `<span class="hint" style="padding:12px;display:block">${
      stageData.T?.status==='done' ? 'No data tables on this sheet.' : 'Run a page first.'}</span>`;
    return;
  }
  $('#tablesList').innerHTML = tablesData.map((t, i) => {
    const h = t.header_rows ?? 1;
    const dis = {};
    for (const d of (t.disputed || [])) dis[d.row + ',' + d.col] = d.readings;
    const grid = t.rows.map((r, ri) => `<tr${ri < h ? ' class="h"' : ''}>${r.map((c, ci) => {
      const d = dis[ri + ',' + ci];
      return d ? `<td class="dispute" title="readers disagree: ${esc(d.join(' | '))}">${esc(c)}</td>` : `<td>${esc(c)}</td>`;
    }).join('')}</tr>`).join('');
    return `<div class="tbl" data-i="${i}">
      <h4>${esc(t.title || 'Table '+(t.index+1))}${t.kind && t.kind !== 'data'
        ? ` <span class="hint" title="Drawing administration, included in case it counts as a table on the sheet">· ${esc(t.kind.replace('_',' '))}</span>` : ''}</h4>
      <div class="hint">${(t.readers||[]).length > 1 ? `read by ${t.readers.map(esc).join(' + ')} · ${
          (t.disputed||[]).length ? `<span style="color:var(--warn)">${t.disputed.length} disputed cell(s) — hover the orange cells</span>` : 'all readers agree'} · ` : ''}${t.n_rows} rows × ${t.n_cols} cols · ${t.read_scale < 1 ? `<span style="color:var(--warn)">read at ${Math.round(t.read_scale*100)}% size</span>` : 'read at full resolution'}
        · <a href="/api/tables/${page}/${t.index}.csv">CSV</a></div>
      <div class="grid"><table>${grid}</table></div>
      ${t.notes ? `<div class="hint" style="margin-top:5px">Model's notes: ${esc(t.notes)}</div>` : ''}
    </div>`;
  }).join('');
  $('#tablesList').querySelectorAll('.tbl').forEach(el => el.onclick = e => {
    if (e.target.tagName === 'A') return;
    const t = tablesData[+el.dataset.i]; hlBox = t.bbox; sel = -1; panTo(t.bbox); draw();
  });
}

/* ---------- rules ---------- */
let rulesBlob = null, ruleCheckRunning = false;
const ruleText = r => {
  const lines = [];
  const kv = (k, v) => lines.push(`${k}: ${typeof v === 'object' ? '' : v}`);
  for (const k of ['category','when','unless','emit','pattern_field','pattern','template','keep_legend_codes']){
    if (r[k] == null) continue;
    if (typeof r[k] === 'object'){ kv(k, r[k]); for (const [f, pat] of Object.entries(r[k])) lines.push(`  ${f}: ${pat}`); }
    else kv(k, r[k]);
  }
  return lines.join('\n');
};
function toast(html, onclick){
  const t = $('#toast'); t.innerHTML = html; t.hidden = false;
  t.onclick = () => { t.hidden = true; if (onclick) onclick(); };
}
function openRulesTab(){ document.querySelector('.tab[data-p=rules]').click(); }

async function loadRules(afterRun){
  const pg = page || $('#doc').value;
  rulesBlob = await (await fetch(`/api/rules?page=${pg}`)).json();
  renderRules();
  // A sheet with no answer key is a sheet our rules were not written for:
  // check them once, automatically, and say so in the corner.
  if (afterRun && lastSummary && !lastSummary.score?.has_key && !rulesBlob.proposals && lastSummary.observations)
    proposeRules(true);
}
async function proposeRules(auto){
  if (ruleCheckRunning) return;
  ruleCheckRunning = true;
  const pg = page || $('#doc').value;
  const cats = $('#ruleCats').value.split(',').map(x => x.trim()).filter(Boolean);
  $('#ruleStatus').textContent = 'Checking rules against this sheet…';
  if (auto) toast('New sheet type — checking whether the rules fit it…');
  const r = await (await fetch('/api/rules/propose', {method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({page: pg, categories: cats})})).json();
  ruleCheckRunning = false;
  if (r.error){ $('#ruleStatus').textContent = r.error; if (auto) $('#toast').hidden = true; return; }
  await loadRules(false);
  const n = (r.proposals||[]).filter(p => p.status === 'pending').length;
  $('#ruleStatus').textContent = r.sheet_summary || '';
  toast(n ? `<b>Rules: ${n} proposed change${n>1?'s':''}</b> for this sheet — click to review` : 'Rules checked — no changes needed for this sheet.', openRulesTab);
}
async function decideRule(id, action){
  const pg = page || $('#doc').value;
  const r = await (await fetch('/api/rules/decide', {method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({page: pg, id, action})})).json();
  if (r.error){ $('#ruleStatus').textContent = r.error; return; }
  await loadRules(false);
  // Accepted rules take effect immediately: re-run (every AI layer comes from cache).
  if (action === 'accept'){ $('#ruleStatus').textContent = 'Rule saved — re-running Layer E from cache…'; runPipeline(false); }
}
function renderRules(){
  const b = rulesBlob || {};
  const props = b.proposals?.proposals || [];
  const pending = props.filter(p => p.status === 'pending').length;
  $('#ruleN').textContent = pending || '–';
  $('#ruleProps').innerHTML = props.length ? `<div class="sec" style="padding-bottom:2px"><h2>Proposed for this sheet${
      b.proposals.created_at ? ` · ${esc(b.proposals.created_at.replace('T',' '))}` : ''}</h2>
      ${b.proposals.sheet_summary ? `<div class="hint">${esc(b.proposals.sheet_summary)}</div>` : ''}</div>` +
    props.map(p => {
      const pv = p.preview || {};
      const keyed = (pv.keyed_sheets||[]).map(k => {
        const d = (k.recall_after??0) - (k.recall_before??0);
        return `page ${k.page}: ${(k.recall_before*100).toFixed(1)}% → <span class="${d<0?'bad':d>0?'good':''}">${(k.recall_after*100).toFixed(1)}%</span>`;
      }).join(' · ');
      return `<div class="rule prop ${p.status}">
        <div class="rh"><b>${p.action === 'add' ? 'Add' : 'Change'} · ${esc(p.rule.category)}</b>
          ${p.action==='modify'?`<span class="ix">rule #${p.index}</span>`:''}<span class="ix" style="margin-left:auto">${esc(p.status)}</span></div>
        <div style="margin-top:3px">${esc(p.reason)}</div>
        <div class="src">from: ${esc(p.source)}${(p.evidence||[]).length ? ' · evidence: ' + p.evidence.map(esc).join(', ') : ''}</div>
        ${p.before ? `<div class="diff"><div><div class="lbl">BEFORE</div><pre>${esc(ruleText(p.before))}</pre></div>
                       <div><div class="lbl">AFTER</div><pre>${esc(ruleText(p.rule))}</pre></div></div>`
                   : `<div class="lbl" style="font-size:10px;color:var(--dim);font-weight:700;margin-top:5px">NEW RULE</div><pre>${esc(ruleText(p.rule))}</pre>`}
        ${p.invalid ? `<div class="pv bad">Can't be applied: ${esc(p.invalid)}</div>` : `<div class="pv">
          On this sheet: ${pv.adds?.length ? `adds <b>${pv.adds.map(esc).join(', ')}</b>` : 'adds nothing'}${pv.removes?.length ? ` · removes <b>${pv.removes.map(esc).join(', ')}</b>` : ''}<br>
          Sheets with answer keys: ${keyed || 'none cached'}${pv.regresses ? ' <span class="bad">— makes an answer-keyed sheet worse</span>' : ''}</div>`}
        ${p.status === 'pending' ? `<div class="q" style="border:0;padding:0;margin:6px 0 0;background:none"><div class="acts">
          <button class="ok" data-a="accept" data-id="${p.id}" ${p.invalid?'disabled':''}>Accept</button>
          <button class="no" data-a="reject" data-id="${p.id}">Reject</button></div></div>` : ''}
      </div>`;
    }).join('') : '';
  $('#ruleProps').querySelectorAll('button[data-a]').forEach(btn =>
    btn.onclick = () => decideRule(+btn.dataset.id, btn.dataset.a));
  $('#ruleList').innerHTML = (b.rules||[]).map((r, i) => `<div class="rule">
      <div class="rh"><b>${esc(r.category)}</b><span class="ix">#${i}</span></div>
      ${r.note ? `<div style="margin-top:2px">${esc(r.note)}</div>` : ''}
      <div class="src">source: ${esc(r.source || '—')}</div>
      <pre>${esc(ruleText(r))}</pre></div>`).join('');
  $('#ruleLog').innerHTML = (b.changelog||[]).map(c => `<div class="rule">
      <div class="rh"><b>${esc(c.action)} · ${esc(c.category)}</b><span class="ix" style="margin-left:auto">${esc((c.at||'').replace('T',' '))}</span></div>
      <div>${esc(c.reason||'')}</div>
      <div class="src">sheet ${esc(c.sheet_id||'?')} · ${esc(c.by||'')} · previous version: ${esc(c.previous_version||'')}</div></div>`).join('')
    || '<div class="hint" style="padding:4px 14px 14px">No changes yet.</div>';
}
$('#ruleCheck').onclick = () => proposeRules(false);

/* ---------- readers (cascade comparison) ---------- */
function renderReaders(){
  const casc = stageData.D?.data?.meta?.cascade;
  const disputed = observations.filter(o => o.source === 'adjudicated' || o.source === 'unsettled');
  const agreed = observations.filter(o => o.source === 'agreed').length;
  $('#rdN').textContent = casc ? disputed.length : '–';
  if (!casc){
    $('#rdStats').innerHTML = '';
    $('#rdList').innerHTML = `<span class="hint" style="padding:12px;display:block">This run used one reader${
      lastSummary?.d_label ? ` (${esc(lastSummary.d_label)})` : ''}. Turn on the cascade (header checkbox) to compare two readers.</span>`;
    return;
  }
  const e = casc.escalation || {}, m = casc.merge || {}, cost = casc.cost || {};
  const pct = v => v == null ? '—' : (v*100).toFixed(0)+'%';
  $('#rdStats').innerHTML = `
    <div><b style="color:var(--ok)">${pct(e.agreement_rate)}</b><span>agreed</span></div>
    <div><b>${e.conflict ?? '—'}</b><span>read differently</span></div>
    <div><b>${(e.solo_a ?? 0) + (e.solo_b ?? 0)}</b><span>seen by one</span></div>
    <div><b>${e.disputes_escalated ?? '—'}</b><span>sent to flagship</span></div>
    <div><b>${m.dropped_as_absent ?? '—'}</b><span>ruled not there</span></div>
    <div><b>${cost.total != null ? '$'+cost.total.toFixed(2) : '—'}</b><span>cost</span></div>
    <div class="hint" style="flex-basis:100%">${agreed} readings both models agreed on are kept as is.
      ${e.flagship_calls ?? 0} flagship call(s) across ${e.tiles_escalated ?? 0} of ${e.tiles_total ?? 0} tiles.
      Only disputes that could change an answer (outside notes, short enough to be a tag) are sent up.</div>`;
  const rows = disputed.slice().sort((a,b) => (a.source > b.source ? 1 : -1));
  $('#rdList').innerHTML = `<div class="rd hd"><span class="h">READER A</span><span class="h">READER B</span><span class="h">RESULT</span></div>` +
    rows.map((o, i) => {
      const a = o._claim_a, b = o._claim_b;
      const res = o.source === 'adjudicated'
        ? `<span class="ruled" title="ruled by ${esc(o.model||'flagship')}">${esc(o.text||'')}</span>`
        : `<span title="not sent to the flagship — the more confident reading is kept">${esc(o.text||'')} <span class="hint">(kept)</span></span>`;
      return `<div class="rd" data-i="${i}"><span>${a ? esc(a) : '<span class="hint">—</span>'}</span>
        <span>${b ? esc(b) : '<span class="hint">—</span>'}</span><span>${res}</span></div>`;
    }).join('');
  $('#rdList').querySelectorAll('.rd[data-i]').forEach(el => el.onclick = () => {
    const o = rows[+el.dataset.i]; hlBox = o.bbox; sel = -1; panTo(o.bbox); draw();
    const [x0,y0,x1,y1] = o.bbox, q = `page=${page}&x0=${x0}&y0=${y0}&x1=${x1}&y1=${y1}`;
    $('#insp').innerHTML = `<img id="crop" src="/api/crop?${q}"><dl class="kv">
      <dt>reader A</dt><dd class="read">${esc(o._claim_a ?? '—')}</dd>
      <dt>reader B</dt><dd class="read">${esc(o._claim_b ?? '—')}</dd>
      <dt>result</dt><dd class="read">${esc(o.text||'')}</dd>
      <dt>settled by</dt><dd>${o.source === 'adjudicated' ? esc(o.model||'flagship') : 'not escalated'}</dd></dl>`;
  });
}

/* ---------- draggable side panel ---------- */
(() => {
  const aside = document.querySelector('aside'), sp = $('#splitter');
  try { const w = +localStorage.getItem('xm.asideW'); if (w > 0) aside.style.width = w + 'px'; } catch {}
  let dragging = false;
  sp.addEventListener('pointerdown', e => { dragging = true; sp.classList.add('on'); sp.setPointerCapture(e.pointerId); });
  sp.addEventListener('pointermove', e => {
    if (!dragging) return;
    const w = Math.min(window.innerWidth * 0.75, Math.max(280, window.innerWidth - e.clientX));
    aside.style.width = w + 'px'; resize();
  });
  sp.addEventListener('pointerup', () => {
    dragging = false; sp.classList.remove('on');
    try { localStorage.setItem('xm.asideW', parseInt(aside.style.width)); } catch {}
  });
})();

// Opening or switching a page shows everything already saved for it, without
// calling any model; Run computes whatever is missing.
$('#doc').addEventListener('change', () => runPipeline(false, true));
// Switching the model or mode shows that setup's saved results at once — free.
for (const id of ['#mB', '#mSingle', '#mT', '#mMode', '#mLightA', '#mLightB', '#mFlag', '#useCascade'])
  $(id).addEventListener('change', () => { if (page) runPipeline(false, true); });
loadDocs().then(() => { $('#doc').onchange(); resize(); runPipeline(false, true); });
loadModels();
