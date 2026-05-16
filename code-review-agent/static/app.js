/* app.js — Shared utilities for all review pages */

// ── API helpers ──────────────────────────────────────────────────
async function apiPost(url, body) {
  const res  = await fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  const data = await res.json();
  if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`);
  return data;
}

async function apiPostForm(url, form) {
  const res  = await fetch(url, { method: 'POST', body: form });
  const data = await res.json();
  if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`);
  return data;
}

async function apiGet(url) {
  const res = await fetch(url);
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.json();
}

// ── Progress step definitions ────────────────────────────────────
var STEPS_CONFIG = {
  pr: [
    { label: '获取 PR 差异内容', completeAt: 8   },
    { label: '解析文件结构',     completeAt: 22  },
    { label: '安全漏洞扫描',     completeAt: 75  },
    { label: '代码质量分析',     completeAt: 125 },
    { label: '汇总生成报告',     completeAt: 155 },
  ],
  diff: [
    { label: '解析 Diff 内容',   completeAt: 5   },
    { label: '识别变更文件',     completeAt: 18  },
    { label: '安全漏洞扫描',     completeAt: 70  },
    { label: '代码质量分析',     completeAt: 120 },
    { label: '汇总生成报告',     completeAt: 150 },
  ],
  upload: [
    { label: '读取文件内容',     completeAt: 3   },
    { label: '安全漏洞扫描',     completeAt: 50  },
    { label: '代码质量分析',     completeAt: 100 },
    { label: '汇总生成报告',     completeAt: 130 },
  ],
};

// ── ReviewManager class ──────────────────────────────────────────
class ReviewManager {
  constructor(mode) {
    this.mode      = mode;
    this.steps     = STEPS_CONFIG[mode] || STEPS_CONFIG.upload;
    this.sessionId = null;
    this._poll     = null;
    this._tick     = null;
    this.startTime = null;
    this.reportTxt = '';

    // Bind DOM refs (present on every review page)
    this.progressWrap = document.getElementById('progress-wrap');
    this.resultWrap   = document.getElementById('result-wrap');
    this.progBar      = document.getElementById('prog-fill');
    this.elapsedEl    = document.getElementById('elapsed');
    this.stepsEl      = document.getElementById('steps');
    this.reportEl     = document.getElementById('report-body');
    this.badgeSec     = document.getElementById('badge-sec');
    this.badgeQual    = document.getElementById('badge-qual');
  }

  // ── Show progress UI ─────────────────────────────────────────
  showProgress() {
    this.startTime = Date.now();
    this.steps     = STEPS_CONFIG[this.mode] || STEPS_CONFIG.upload;

    if (this.resultWrap)   this.resultWrap.classList.remove('on');
    if (this.progressWrap) this.progressWrap.classList.add('on');

    // Build step list
    if (this.stepsEl) {
      this.stepsEl.innerHTML = this.steps.map((s, i) =>
        `<div class="step${i === 0 ? ' active' : ''}" id="step-${i}">
           <div class="step-dot"></div>
           <span>${s.label}</span>
         </div>`
      ).join('');
    }

    // Start tick
    this._tick = setInterval(() => this._onTick(), 1000);
  }

  _onTick() {
    const elapsed = Math.floor((Date.now() - this.startTime) / 1000);
    if (this.elapsedEl) this.elapsedEl.textContent = elapsed + 's';

    // Advance steps
    this.steps.forEach((s, i) => {
      const el = document.getElementById(`step-${i}`);
      if (!el || el.classList.contains('done')) return;
      if (elapsed >= s.completeAt) {
        el.classList.remove('active');
        el.classList.add('done');
        const next = document.getElementById(`step-${i + 1}`);
        if (next && !next.classList.contains('done')) next.classList.add('active');
      }
    });

    // Progress bar (asymptotic, never hits 100% until done)
    const lastAt = this.steps[this.steps.length - 1].completeAt;
    const pct    = Math.min(93, Math.round((elapsed / (lastAt + 25)) * 100));
    if (this.progBar) this.progBar.style.width = pct + '%';
  }

  // ── Start polling ─────────────────────────────────────────────
  startPolling(sessionId) {
    this.sessionId = sessionId;
    this._poll = setInterval(() => this._doPoll(), 2500);
  }

  async _doPoll() {
    try {
      const data = await apiGet(`/api/review/${this.sessionId}`);
      if (data.status === 'done')  this._done(data);
      if (data.status === 'error') this._error(data.error);
    } catch { /* network hiccup – keep polling */ }
  }

  _done(data) {
    this._clearTimers();
    // Complete all steps
    this.steps.forEach((_, i) => {
      const el = document.getElementById(`step-${i}`);
      if (el) { el.classList.remove('active'); el.classList.add('done'); }
    });
    if (this.progBar) this.progBar.style.width = '100%';

    setTimeout(() => {
      if (this.progressWrap) this.progressWrap.classList.remove('on');
      this._renderResults(data.report, data.stats);
    }, 700);
  }

  _error(msg) {
    this._clearTimers();
    if (this.progressWrap) this.progressWrap.classList.remove('on');
    this.showError(msg || '审查失败，请重试');
  }

  // ── Render results ────────────────────────────────────────────
  _renderResults(report, stats) {
    this.reportTxt = report || '';

    if (stats) {
      if (this.badgeSec) {
        this.badgeSec.textContent  = stats.security + ' 安全';
        this.badgeSec.className    = 'badge ' + (stats.security > 0 ? 'badge-red' : 'badge-green');
      }
      if (this.badgeQual) this.badgeQual.textContent = stats.quality + ' 质量';
    }

    if (this.reportEl && window.marked) {
      this.reportEl.innerHTML = marked.parse(this.reportTxt);
      if (window.hljs) {
        this.reportEl.querySelectorAll('pre code').forEach(el => hljs.highlightElement(el));
      }
    }

    if (this.resultWrap) {
      this.resultWrap.classList.add('on');
      this.resultWrap.scrollIntoView({ behavior: 'smooth', block: 'start' });
    }
  }

  // ── Error display ─────────────────────────────────────────────
  showError(msg, targetId) {
    const el = document.getElementById(targetId || 'error-msg');
    if (el) { el.textContent = '⚠  ' + msg; el.classList.add('on'); }
  }

  hideError(targetId) {
    const el = document.getElementById(targetId || 'error-msg');
    if (el) el.classList.remove('on');
  }

  // ── Copy report ───────────────────────────────────────────────
  async copyReport(btn) {
    if (!this.reportTxt) return;
    try {
      await navigator.clipboard.writeText(this.reportTxt);
      btn.classList.add('ok');
      btn.textContent = '✓ 已复制';
      setTimeout(() => {
        btn.classList.remove('ok');
        btn.textContent = '复制';
      }, 2000);
    } catch { /* ignore */ }
  }

  // ── Download report ───────────────────────────────────────────
  downloadReport() {
    if (!this.reportTxt) return;
    const blob = new Blob([this.reportTxt], { type: 'text/markdown;charset=utf-8' });
    const url  = URL.createObjectURL(blob);
    const a    = Object.assign(document.createElement('a'), {
      href: url, download: `code-review-${Date.now()}.md`,
    });
    a.click();
    URL.revokeObjectURL(url);
  }

  _clearTimers() {
    clearInterval(this._poll);
    clearInterval(this._tick);
  }
}
