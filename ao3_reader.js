document.documentElement.classList.add('js');
const themeKey = 'ao3-reader-theme';
const fontKey = 'ao3-reader-font';
const sizeKey = 'ao3-reader-size';
const widthKey = 'ao3-reader-width';
const themes = ['system', 'day', 'sage', 'lavender', 'sepia', 'crimson', 'night', 'dusk', 'midnight'];
const fonts = ['serif', 'sans', 'kaiti', 'system'];
const sizes = ['small', 'medium', 'large', 'xlarge'];
const widths = ['narrow', 'medium', 'wide'];
const systemTheme = window.matchMedia('(prefers-color-scheme: dark)');

function readPref(key, allowed, fallback) {
  try {
    const saved = localStorage.getItem(key);
    if (allowed.includes(saved)) return saved;
  } catch {
    // Storage may be blocked while keeping defaults for the current view.
  }
  return fallback;
}

function savePref(key, value) {
  try {
    localStorage.setItem(key, value);
  } catch {
    // Keep the selected setting on the current page.
  }
}

let theme = readPref(themeKey, themes, 'system');
let fontFamily = readPref(fontKey, fonts, 'serif');
let fontSize = readPref(sizeKey, sizes, 'medium');
let pageWidth = readPref(widthKey, widths, 'medium');

function applyPrefs() {
  const preferred = systemTheme.matches ? 'night' : 'day';
  document.documentElement.dataset.readerTheme = theme === 'system' ? preferred : theme;
  document.documentElement.dataset.readerFont = fontFamily;
  document.documentElement.dataset.readerSize = fontSize;
  document.documentElement.dataset.readerWidth = pageWidth;
}

applyPrefs();
systemTheme.addEventListener('change', applyPrefs);

document.addEventListener('DOMContentLoaded', () => {
  const bindSelect = (selector, current, setter, key) => {
    const el = document.querySelector(selector);
    if (!el) return;
    el.value = current;
    el.addEventListener('change', () => {
      setter(el.value);
      applyPrefs();
      savePref(key, el.value);
    });
  };
  bindSelect('#reader-theme', theme, v => { theme = v; }, themeKey);
  bindSelect('#reader-font', fontFamily, v => { fontFamily = v; }, fontKey);
  bindSelect('#reader-size', fontSize, v => { fontSize = v; }, sizeKey);
  bindSelect('#reader-width', pageWidth, v => { pageWidth = v; }, widthKey);

  // Download dropdown menus: keep only one open and close on outside click / Escape / pick.
  const menus = Array.from(document.querySelectorAll('.download-menu'));
  if (menus.length) {
    for (const menu of menus) {
      menu.addEventListener('toggle', () => {
        if (!menu.open) return;
        for (const other of menus) {
          if (other !== menu) other.open = false;
        }
      });
      menu.addEventListener('click', event => {
        if (event.target.closest('a[role="menuitem"]')) {
          menu.open = false;
        }
      });
    }
    document.addEventListener('click', event => {
      if (!event.target.closest('.download-menu')) {
        for (const menu of menus) menu.open = false;
      }
    });
    document.addEventListener('keydown', event => {
      if (event.key === 'Escape') {
        for (const menu of menus) menu.open = false;
      }
    });
  }

  // Reader chapter & top quick navigation.
  const toc = document.querySelector('#toc');
  const openToc = () => {
    if (!toc) return;
    toc.open = true;
    toc.scrollIntoView({ behavior: 'smooth', block: 'start' });
  };
  const tocBtn = document.querySelector('#reader-toc-btn');
  if (tocBtn) tocBtn.addEventListener('click', openToc);
  for (const link of document.querySelectorAll('.toc-jump')) {
    link.addEventListener('click', event => {
      if (!toc) return;
      event.preventDefault();
      openToc();
    });
  }
  const topBtn = document.querySelector('#reader-top-btn');
  if (topBtn) {
    topBtn.addEventListener('click', () => {
      window.scrollTo({ top: 0, behavior: 'smooth' });
    });
  }

  // Diff expand/collapse all button.
  const diffToggle = document.querySelector('.diff-toggle-all');
  if (diffToggle) {
    const diffs = Array.from(document.querySelectorAll('.diffs details.diff'));
    if (diffs.length > 1) {
      diffToggle.hidden = false;
      const syncLabel = () => {
        const allOpen = diffs.every(d => d.open);
        diffToggle.textContent = allOpen ? '收起全部差异' : '展开全部差异';
      };
      diffToggle.addEventListener('click', () => {
        const openNext = diffs.some(d => !d.open);
        for (const d of diffs) d.open = openNext;
        syncLabel();
      });
      for (const d of diffs) d.addEventListener('toggle', syncLabel);
      syncLabel();
    }
  }

  // Backup schedule dynamic panel + real-time next-runs preview.
  const schedForm = document.querySelector('#backup-settings-form');
  if (schedForm) {
    const modeRadios = Array.from(schedForm.querySelectorAll('input[name="schedule_mode"]'));
    const panels = Array.from(schedForm.querySelectorAll('[data-schedule-panel]'));
    const runAtWrap = document.querySelector('#backup-run-at-wrap');
    const runAtTitle = document.querySelector('#backup-run-at-title');
    const runAtInput = document.querySelector('#backup-run-at');
    const runAtHint = document.querySelector('#backup-run-at-hint');
    const intervalVal = document.querySelector('#backup-interval-value');
    const intervalUnit = document.querySelector('#backup-interval-unit');
    const anchorMode = document.querySelector('#backup-anchor-mode');
    const anchorHint = document.querySelector('#backup-anchor-hint');
    const monthDay = document.querySelector('#backup-month-day');
    const weekdayBoxes = Array.from(schedForm.querySelectorAll('input[name="weekdays"]'));
    const previewEl = document.querySelector('#backup-schedule-preview');

    const unitSec = { minute: 60, hour: 3600, day: 86400 };
    const runAtTitles = {
      interval: ['基准时刻', '固定按此时刻对齐间隔网格，避免越跑越晚'],
      daily: ['每日执行时间', '每天固定在此时刻启动扫描'],
      weekly: ['执行时间', '所选星期几的此时刻启动扫描'],
      monthly: ['执行时间', '每月所选日期的此时刻启动扫描'],
      manual: ['基准时刻', ''],
    };

    const parseHHMM = raw => {
      const m = /^(\d{1,2}):(\d{2})$/.exec((raw || '').trim());
      if (!m) return [3, 30];
      const h = Number(m[1]);
      const min = Number(m[2]);
      if (h < 0 || h > 23 || min < 0 || min > 59) return [3, 30];
      return [h, min];
    };

    const fmtRun = (dt, now) => {
      const pad = n => String(n).padStart(2, '0');
      const hhmm = `${pad(dt.getHours())}:${pad(dt.getMinutes())}`;
      const todayUtc = Date.UTC(now.getFullYear(), now.getMonth(), now.getDate());
      const dtUtc = Date.UTC(dt.getFullYear(), dt.getMonth(), dt.getDate());
      const dayDiff = Math.round((dtUtc - todayUtc) / 86400000);
      if (dayDiff === 0) return `今天 ${hhmm}`;
      if (dayDiff === 1) return `明天 ${hhmm}`;
      if (dt.getFullYear() === now.getFullYear()) {
        return `${pad(dt.getMonth() + 1)}-${pad(dt.getDate())} ${hhmm}`;
      }
      return `${dt.getFullYear()}-${pad(dt.getMonth() + 1)}-${pad(dt.getDate())} ${hhmm}`;
    };

    const computePreview = mode => {
      const now = new Date();
      const [hh, mm] = parseHHMM(runAtInput ? runAtInput.value : '03:30');
      if (mode === 'manual') {
        return '当前已关闭自动定时，仅通过「立即刷新」或「重试失败或缺失项」手动触发。';
      }
      const runs = [];
      if (mode === 'daily') {
        for (let offset = 0; runs.length < 3 && offset < 10; offset += 1) {
          const cand = new Date(now.getFullYear(), now.getMonth(), now.getDate() + offset, hh, mm, 0, 0);
          if (cand > now) runs.push(cand);
        }
      } else if (mode === 'weekly') {
        const activeDays = new Set(
          weekdayBoxes.filter(box => box.checked).map(box => Number(box.value))
        );
        if (!activeDays.size) {
          return '请至少选择一个星期，即可预览接下来的执行时间。';
        }
        for (let offset = 0; runs.length < 3 && offset < 28; offset += 1) {
          const cand = new Date(now.getFullYear(), now.getMonth(), now.getDate() + offset, hh, mm, 0, 0);
          const pyDay = (cand.getDay() + 6) % 7;
          if (activeDays.has(pyDay) && cand > now) runs.push(cand);
        }
      } else if (mode === 'monthly') {
        const targetDay = Math.min(31, Math.max(1, Number(monthDay ? monthDay.value : 1) || 1));
        let year = now.getFullYear();
        let month = now.getMonth();
        for (let i = 0; runs.length < 3 && i < 12; i += 1) {
          const lastDay = new Date(year, month + 1, 0).getDate();
          const day = Math.min(targetDay, lastDay);
          const cand = new Date(year, month, day, hh, mm, 0, 0);
          if (cand > now) runs.push(cand);
          month += 1;
          if (month > 11) {
            year += 1;
            month = 0;
          }
        }
      } else if (mode === 'interval') {
        const val = Number(intervalVal ? intervalVal.value : 0);
        const unit = intervalUnit ? intervalUnit.value : 'hour';
        const anchor = anchorMode ? anchorMode.value : 'fixed';
        if (!Number.isFinite(val) || val <= 0) {
          return '请输入大于 0 的间隔数字以预览计划时间。';
        }
        const seconds = val * (unitSec[unit] || 3600);
        if (seconds < 60 || seconds > 366 * 86400) {
          return '抓取间隔应在 1 分钟至 366 天之间。';
        }
        const stepMs = seconds * 1000;
        if (anchor === 'after') {
          for (let i = 1; i <= 3; i += 1) {
            runs.push(new Date(now.getTime() + stepMs * i));
          }
        } else if (unit === 'day' && Number.isInteger(val) && Math.round(seconds) % 86400 === 0) {
          const stepDays = Math.max(1, val);
          const originUtc = Date.UTC(2026, 0, 1);
          const nowUtc = Date.UTC(now.getFullYear(), now.getMonth(), now.getDate());
          let k = Math.max(0, Math.floor((nowUtc - originUtc) / 86400000 / stepDays));
          while (runs.length < 3) {
            const cand = new Date(2026, 0, 1 + k * stepDays, hh, mm, 0, 0);
            if (cand > now) runs.push(cand);
            k += 1;
          }
        } else {
          const base = seconds <= 86400 && Number.isInteger(seconds) && 86400 % seconds === 0
            ? new Date(now.getFullYear(), now.getMonth(), now.getDate(), hh, mm, 0, 0)
            : new Date(2026, 0, 1, hh, mm, 0, 0);
          let k = Math.floor((now.getTime() - base.getTime()) / stepMs) + 1;
          while (new Date(base.getTime() + k * stepMs) <= now) {
            k += 1;
          }
          for (let i = 0; i < 3; i += 1) {
            runs.push(new Date(base.getTime() + (k + i) * stepMs));
          }
        }
      }
      if (!runs.length) return '';
      return `按当前计划，预计接下来 ${runs.length} 次：${runs.map(r => fmtRun(r, now)).join(' · ')}`;
    };

    const syncScheduleForm = () => {
      const checked = modeRadios.find(r => r.checked);
      const mode = checked ? checked.value : 'daily';
      const anchor = anchorMode ? anchorMode.value : 'fixed';
      for (const panel of panels) {
        panel.hidden = panel.dataset.schedulePanel !== mode;
      }
      if (runAtWrap) {
        runAtWrap.hidden = mode === 'manual' || (mode === 'interval' && anchor === 'after');
      }
      if (runAtTitle && runAtHint && runAtTitles[mode]) {
        runAtTitle.textContent = runAtTitles[mode][0];
        runAtHint.textContent = runAtTitles[mode][1];
      }
      if (anchorHint) {
        anchorHint.textContent = anchor === 'fixed'
          ? '以基准时刻切分固定周期，不受单轮耗时影响'
          : '待上轮完成后再等待对应间隔，起始点逐次顺延';
      }
      if (previewEl) {
        previewEl.textContent = computePreview(mode);
      }
    };

    schedForm.addEventListener('change', syncScheduleForm);
    schedForm.addEventListener('input', syncScheduleForm);
    syncScheduleForm();
  }

  // Overview card summary: auto-detect overflow for fade clamp + expand toggle.
  const summaryWraps = Array.from(document.querySelectorAll('.card-summary-wrap'));
  if (summaryWraps.length) {
    const syncSummary = wrap => {
      const text = wrap.querySelector('.card-summary');
      const btn = wrap.querySelector('.summary-toggle');
      if (!text || !btn) return;
      if (wrap.dataset.expanded === '1') {
        wrap.classList.remove('is-collapsed');
        btn.hidden = false;
        btn.setAttribute('aria-expanded', 'true');
        btn.setAttribute('aria-label', '收起简介');
        btn.title = '收起简介';
        return;
      }
      wrap.classList.add('is-collapsed');
      const overflows = text.scrollHeight > text.clientHeight + 3;
      if (overflows) {
        btn.hidden = false;
        btn.setAttribute('aria-expanded', 'false');
        btn.setAttribute('aria-label', '展开简介');
        btn.title = '展开简介';
      } else {
        wrap.classList.remove('is-collapsed');
        btn.hidden = true;
      }
    };

    for (const wrap of summaryWraps) {
      const btn = wrap.querySelector('.summary-toggle');
      if (btn) {
        btn.addEventListener('click', () => {
          wrap.dataset.expanded = wrap.dataset.expanded === '1' ? '0' : '1';
          syncSummary(wrap);
        });
      }
      syncSummary(wrap);
    }

    let resizeTimer = 0;
    window.addEventListener('resize', () => {
      window.cancelAnimationFrame(resizeTimer);
      resizeTimer = window.requestAnimationFrame(() => {
        for (const wrap of summaryWraps) {
          if (wrap.dataset.expanded !== '1') syncSummary(wrap);
        }
      });
    });
  }
});
