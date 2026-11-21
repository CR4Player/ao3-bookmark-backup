document.documentElement.classList.add('js');
const themeKey = 'ao3-reader-theme';
const fontKey = 'ao3-reader-font';
const sizeKey = 'ao3-reader-size';
const widthKey = 'ao3-reader-width';
const densityKey = 'ao3-reader-density';
const sidebarKey = 'ao3-reader-sidebar';
const themes = ['system', 'day', 'sage', 'lavender', 'sepia', 'crimson', 'night', 'dusk', 'midnight'];
const fonts = ['serif', 'sans', 'kaiti', 'system'];
const sizes = ['small', 'medium', 'large', 'xlarge'];
const widths = ['narrow', 'medium', 'wide'];
const densities = ['compact', 'cozy'];
const sidebars = ['open', 'collapsed'];
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
let listDensity = readPref(densityKey, densities, 'compact');
let readerSidebar = readPref(sidebarKey, sidebars, 'open');

function applyPrefs() {
  const preferred = systemTheme.matches ? 'night' : 'day';
  document.documentElement.dataset.readerTheme = theme === 'system' ? preferred : theme;
  document.documentElement.dataset.readerFont = fontFamily;
  document.documentElement.dataset.readerSize = fontSize;
  document.documentElement.dataset.readerWidth = pageWidth;
  document.documentElement.dataset.readerDensity = listDensity;
  document.documentElement.dataset.readerSidebar = readerSidebar;
}

function switchThemeInstant() {
  const root = document.documentElement;
  root.classList.add('is-theme-switching');
  applyPrefs();
  void root.offsetHeight;
  root.classList.remove('is-theme-switching');
}

applyPrefs();
systemTheme.addEventListener('change', switchThemeInstant);

document.addEventListener('DOMContentLoaded', () => {
  const bindSelect = (selector, current, setter, key, onApply = applyPrefs) => {
    const el = document.querySelector(selector);
    if (!el) return;
    el.value = current;
    el.addEventListener('change', () => {
      setter(el.value);
      onApply();
      savePref(key, el.value);
    });
  };
  bindSelect('#reader-theme', theme, v => { theme = v; }, themeKey, switchThemeInstant);
  bindSelect('#reader-font', fontFamily, v => { fontFamily = v; }, fontKey);
  bindSelect('#reader-size', fontSize, v => { fontSize = v; }, sizeKey);
  bindSelect('#reader-width', pageWidth, v => { pageWidth = v; }, widthKey);

  // Overview list density switcher (compact / cozy).
  const densityBtns = Array.from(document.querySelectorAll('.density-switch [data-density]'));
  if (densityBtns.length) {
    const syncDensityBtns = () => {
      for (const btn of densityBtns) {
        btn.classList.toggle('is-active', btn.dataset.density === listDensity);
      }
    };
    for (const btn of densityBtns) {
      btn.addEventListener('click', () => {
        const val = btn.dataset.density;
        if (densities.includes(val)) {
          listDensity = val;
          applyPrefs();
          savePref(densityKey, val);
          syncDensityBtns();
        }
      });
    }
    syncDensityBtns();
  }

  // Reader sidebar collapse / expand toggle & reading progress bar.
  const sidebarToggle = document.querySelector('#reader-sidebar-toggle');
  const syncSidebarBtn = () => {
    if (!sidebarToggle) return;
    const collapsed = readerSidebar === 'collapsed';
    sidebarToggle.textContent = collapsed ? '⇥ 展开侧栏' : '⇤ 收起侧栏';
    sidebarToggle.setAttribute('aria-expanded', collapsed ? 'false' : 'true');
  };
  const setSidebarState = next => {
    readerSidebar = next === 'collapsed' ? 'collapsed' : 'open';
    applyPrefs();
    savePref(sidebarKey, readerSidebar);
    syncSidebarBtn();
  };
  if (sidebarToggle) {
    sidebarToggle.addEventListener('click', () => {
      setSidebarState(readerSidebar === 'collapsed' ? 'open' : 'collapsed');
    });
    syncSidebarBtn();
  }

  const progressBar = document.querySelector('#reading-progress');
  const chapterEls = Array.from(document.querySelectorAll('.article-chapter[id]'));
  const tocLinks = new Map(
    Array.from(document.querySelectorAll('.toc-list a[href^="#"]')).map(a => [a.getAttribute('href').slice(1), a])
  );
  if (progressBar || chapterEls.length > 1) {
    let scrollTick = false;
    const updateReadingProgress = () => {
      scrollTick = false;
      const docEl = document.documentElement;
      const maxScroll = Math.max(1, docEl.scrollHeight - window.innerHeight);
      const ratio = Math.min(1, Math.max(0, window.scrollY / maxScroll));
      if (progressBar) {
        progressBar.style.width = `${(ratio * 100).toFixed(1)}%`;
      }
      if (chapterEls.length > 1 && tocLinks.size) {
        let activeId = chapterEls[0].id;
        for (const ch of chapterEls) {
          if (ch.getBoundingClientRect().top <= 140) {
            activeId = ch.id;
          } else {
            break;
          }
        }
        for (const [id, link] of tocLinks.entries()) {
          link.classList.toggle('is-current', id === activeId);
        }
      }
    };
    window.addEventListener('scroll', () => {
      if (!scrollTick) {
        scrollTick = true;
        window.requestAnimationFrame(updateReadingProgress);
      }
    }, { passive: true });
    updateReadingProgress();
  }

  // Auto-apply overview sort selection and search clear without requiring a separate button.
  for (const sel of document.querySelectorAll('select[data-auto-submit]')) {
    sel.addEventListener('change', () => {
      if (sel.form) sel.form.submit();
    });
  }
  const searchInput = document.querySelector('.search input[type="search"]');
  if (searchInput && searchInput.form) {
    const initialQuery = searchInput.value;
    searchInput.addEventListener('search', () => {
      if (searchInput.value !== initialQuery) searchInput.form.submit();
    });
  }

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
    if (readerSidebar === 'collapsed') {
      setSidebarState('open');
    }
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

  // AO3 auth mode slider & async credential verification in /account.
  let runAo3Logout = null;
  for (const scope of document.querySelectorAll('[data-ao3-auth-scope]')) {
    const slider = scope.querySelector('.auth-slider');
    const radios = Array.from(scope.querySelectorAll('input[name="ao3_auth_mode"]'));
    const fieldsBox = scope.querySelector('[data-ao3-cred-fields]');
    const userInput = scope.querySelector('input[name="ao3_user"]');
    const pwInput = scope.querySelector('input[name="ao3_password"]');
    const modeHint = scope.querySelector('[data-ao3-mode-hint]');
    const pwHint = scope.querySelector('[data-ao3-pw-hint]');

    const form = scope.closest('[data-ao3-cred-form]');
    const liveBox = document.querySelector('#ao3-cred-live');
    const liveText = document.querySelector('[data-cred-live-text]');
    const submitBtn = form ? form.querySelector('[data-cred-submit]') : null;
    const cancelBtn = form ? form.querySelector('[data-cred-cancel]') : null;
    const ao3LogoutBtn = form ? form.querySelector('[data-ao3-logout-btn]') : null;
    const statusBadge = document.querySelector('#ao3-cred-badge');
    const envTip = document.querySelector('#ao3-env-tip');
    const flashMsg = document.querySelector('#account-flash-msg');

    const hints = {
      public: '不登录 AO3，仅备份目标用户的公开书签与公开作品。',
      encrypted: '使用密钥文件（/data/.credentials.key）加密存库并支持自动重登；保存前必须联网校验。',
      cookie: '现场向 AO3 登录换取 Cookie 保存，密码不入库；Cookie 过期后需回此页重登。',
    };

    const syncAuth = () => {
      const idx = Math.max(0, radios.findIndex(r => r.checked));
      const mode = radios[idx] ? radios[idx].value : 'public';
      const isPublic = mode === 'public';
      if (slider) slider.dataset.activeIndex = String(idx);
      if (fieldsBox) fieldsBox.hidden = isPublic;
      if (modeHint) modeHint.textContent = hints[mode] || '';
      if (userInput) userInput.required = !isPublic;
      if (pwInput) {
        const hasSaved = pwInput.dataset.hasSaved === '1';
        if (mode === 'cookie') {
          pwInput.placeholder = '输入 AO3 密码用于本次在线登录';
          pwInput.required = true;
          if (pwHint) pwHint.textContent = '仅用于即时登录换取 Cookie，不写入数据库';
        } else if (mode === 'encrypted') {
          pwInput.placeholder = hasSaved ? '已加密保存（留空沿用原密码）' : '输入 AO3 登录密码';
          pwInput.required = !hasSaved;
          if (pwHint) pwHint.textContent = hasSaved ? '更换密码请直接输入；留空则使用已保存密码联网重验' : '通过 Fernet 加密存储并用于自动重登';
        } else {
          pwInput.required = false;
          if (pwHint) pwHint.textContent = '';
        }
      }
      if (submitBtn && !submitBtn.disabled) {
        submitBtn.textContent = isPublic ? '保存设置' : '保存并校验';
      }
    };

    scope.addEventListener('change', syncAuth);
    syncAuth();

    if (form && liveBox && liveText && submitBtn) {
      let pollTimer = 0;
      let pollErrors = 0;
      let pollBusy = false;

      const stopPolling = () => {
        if (pollTimer) {
          window.clearInterval(pollTimer);
          pollTimer = 0;
        }
      };

      const renderJob = data => {
        const state = data.state || 'idle';
        const isRunning = state === 'running';
        const tone = data.tone || (state === 'failed' ? 'error' : state === 'succeeded' ? 'ok' : 'info');
        const msg = data.message || '';

        if (msg) {
          liveBox.hidden = false;
          liveText.textContent = msg;
          liveBox.classList.remove('is-info', 'is-warn', 'is-error', 'is-ok', 'is-running');
          liveBox.classList.add(`is-${tone}`);
          if (isRunning) liveBox.classList.add('is-running');
          liveBox.setAttribute('role', tone === 'error' ? 'alert' : 'status');
        } else {
          liveBox.hidden = true;
        }

        if (data.status_text && statusBadge) {
          statusBadge.textContent = data.status_text;
          statusBadge.className = data.status_class ? `cred-badge ${data.status_class}` : 'cred-badge';
        }
        if (data.configured && envTip) {
          envTip.hidden = true;
        }

        if (isRunning) {
          submitBtn.disabled = true;
          submitBtn.textContent = '校验中…';
          if (cancelBtn) {
            cancelBtn.hidden = false;
            cancelBtn.disabled = false;
          }
          if (ao3LogoutBtn) ao3LogoutBtn.hidden = true;
        } else {
          submitBtn.disabled = false;
          if (cancelBtn) cancelBtn.hidden = true;
          if (ao3LogoutBtn && 'ao3_logged_in' in data) {
            ao3LogoutBtn.hidden = !data.ao3_logged_in;
          }
          if (state === 'succeeded') {
            if (pwInput) {
              pwInput.dataset.hasSaved = (data.auth_mode === 'encrypted' && data.has_password) ? '1' : '0';
              pwInput.value = '';
            }
            if (data.auth_mode === 'public' && data._fromAo3Logout) {
              const pubRadio = radios.find(r => r.value === 'public');
              if (pubRadio) pubRadio.checked = true;
              if (userInput) userInput.value = '';
            }
          }
          syncAuth();
        }
      };

      const fetchStatus = async () => {
        if (pollBusy) return;
        pollBusy = true;
        try {
          const res = await fetch('/account/credentials/status', { headers: { 'X-Requested-With': 'fetch' } });
          const data = await res.json();
          pollErrors = 0;
          if (!res.ok) {
            stopPolling();
            renderJob({ state: 'failed', tone: 'error', message: data.message || '登录状态已失效，请刷新页面重新登录' });
            return;
          }
          renderJob(data);
          if (data.state !== 'running') {
            stopPolling();
          }
        } catch {
          pollErrors += 1;
          if (pollErrors >= 6) {
            stopPolling();
            renderJob({ state: 'failed', tone: 'error', message: '无法获取后台校验进度，请检查网络连接或刷新页面确认结果。' });
          }
        } finally {
          pollBusy = false;
        }
      };

      const startPolling = () => {
        stopPolling();
        pollErrors = 0;
        pollTimer = window.setInterval(fetchStatus, 700);
        fetchStatus();
      };

      form.addEventListener('submit', async event => {
        event.preventDefault();
        if (flashMsg) flashMsg.hidden = true;
        stopPolling();
        submitBtn.disabled = true;
        submitBtn.textContent = '提交中…';
        renderJob({ state: 'running', tone: 'info', message: '正在提交校验任务到后台…' });

        try {
          const body = new URLSearchParams(new FormData(form));
          const res = await fetch(form.getAttribute('action') || '/account', {
            method: 'POST',
            headers: { 'X-Requested-With': 'fetch' },
            body,
          });
          let data = {};
          try {
            data = await res.json();
          } catch {
            data = { state: 'failed', tone: 'error', message: `服务器响应异常（HTTP ${res.status}）` };
          }
          if (!res.ok && data.state !== 'running') {
            renderJob({ state: 'failed', tone: 'error', message: data.message || `请求失败（HTTP ${res.status}）` });
            return;
          }
          renderJob(data);
          if (data.state === 'running') {
            startPolling();
          }
        } catch {
          renderJob({ state: 'failed', tone: 'error', message: '提交校验请求失败：无法连接服务或连接中断，请重试。' });
        }
      });

      if (cancelBtn) {
        cancelBtn.addEventListener('click', async () => {
          cancelBtn.disabled = true;
          stopPolling();
          try {
            const proofInput = form.querySelector('input[name="proof"]');
            const body = new URLSearchParams({ proof: proofInput ? proofInput.value : '' });
            const res = await fetch('/account/credentials/cancel', {
              method: 'POST',
              headers: { 'X-Requested-With': 'fetch' },
              body,
            });
            const data = await res.json();
            renderJob(data);
          } catch {
            renderJob({ state: 'failed', tone: 'error', message: '取消请求发送失败，请刷新页面查看状态。' });
          }
        });
      }

      if (form.dataset.credJobState === 'running') {
        startPolling();
      }

      runAo3Logout = async targetForm => {
        if (flashMsg) flashMsg.hidden = true;
        stopPolling();
        try {
          const res = await fetch(targetForm.getAttribute('action') || '/account/ao3-logout', {
            method: 'POST',
            headers: { 'X-Requested-With': 'fetch' },
            body: new URLSearchParams(new FormData(targetForm)),
          });
          const data = await res.json();
          if (!res.ok) {
            renderJob({ state: 'failed', tone: 'error', message: data.message || '退出 AO3 账号失败，请重试。' });
            return;
          }
          renderJob({ ...data, _fromAo3Logout: true });
        } catch {
          renderJob({ state: 'failed', tone: 'error', message: '无法连接服务端执行退出操作，请刷新后重试。' });
        }
      };
    }
  }

  // Global floating secondary confirmation popover (top-right site logout & card AO3 logout).
  const popover = document.querySelector('#confirm-popover');
  const popTitle = popover ? popover.querySelector('#confirm-title') : null;
  const popBody = popover ? popover.querySelector('#confirm-body') : null;
  const popCancel = popover ? popover.querySelector('[data-confirm-cancel]') : null;
  const popAccept = popover ? popover.querySelector('[data-confirm-accept]') : null;
  if (popover && popTitle && popBody && popCancel && popAccept) {
    let activeTrigger = null;

    const closePopover = () => {
      popover.hidden = true;
      if (activeTrigger) activeTrigger.setAttribute('aria-expanded', 'false');
      activeTrigger = null;
    };

    const positionPopover = trigger => {
      const btnRect = trigger.getBoundingClientRect();
      popover.hidden = false;
      const popRect = popover.getBoundingClientRect();
      const margin = 12;
      let left = btnRect.right > window.innerWidth / 2
        ? btnRect.right - popRect.width
        : btnRect.left;
      left = Math.max(margin, Math.min(left, window.innerWidth - popRect.width - margin));
      let top = btnRect.bottom + 8;
      if (top + popRect.height > window.innerHeight - margin && btnRect.top - popRect.height - 8 >= margin) {
        top = btnRect.top - popRect.height - 8;
      }
      popover.style.left = `${Math.round(left)}px`;
      popover.style.top = `${Math.round(top)}px`;
    };

    for (const trigger of document.querySelectorAll('[data-confirm-trigger]')) {
      trigger.setAttribute('aria-haspopup', 'dialog');
      trigger.setAttribute('aria-expanded', 'false');
      trigger.addEventListener('click', event => {
        event.preventDefault();
        event.stopPropagation();
        if (!popover.hidden && activeTrigger === trigger) {
          closePopover();
          return;
        }
        if (activeTrigger) activeTrigger.setAttribute('aria-expanded', 'false');
        activeTrigger = trigger;
        trigger.setAttribute('aria-expanded', 'true');
        popTitle.textContent = trigger.dataset.confirmTitle || '确认执行操作？';
        popBody.textContent = trigger.dataset.confirmBody || '';
        popAccept.textContent = trigger.dataset.confirmOk || '确认';
        popAccept.disabled = false;
        positionPopover(trigger);
        popCancel.focus();
      });
    }

    popCancel.addEventListener('click', () => {
      const prev = activeTrigger;
      closePopover();
      if (prev) prev.focus();
    });

    popAccept.addEventListener('click', async () => {
      const trigger = activeTrigger;
      if (!trigger) return;
      const targetForm = trigger.form || document.getElementById(trigger.getAttribute('form') || '');
      if (!targetForm) {
        closePopover();
        return;
      }
      if (targetForm.id === 'ao3-logout-form' && runAo3Logout) {
        popAccept.disabled = true;
        closePopover();
        await runAo3Logout(targetForm);
        return;
      }
      closePopover();
      HTMLFormElement.prototype.submit.call(targetForm);
    });

    document.addEventListener('click', event => {
      if (popover.hidden) return;
      if (popover.contains(event.target)) return;
      if (activeTrigger && activeTrigger.contains(event.target)) return;
      closePopover();
    });
    document.addEventListener('keydown', event => {
      if (event.key === 'Escape' && !popover.hidden) {
        const prev = activeTrigger;
        closePopover();
        if (prev) prev.focus();
      }
    });
    window.addEventListener('resize', closePopover);
  }
});
