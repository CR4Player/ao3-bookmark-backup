const logPanel = document.querySelector('#backup-logs');
const logPre = document.querySelector('#backup-log');
const statusBox = document.querySelector('#backup-status');
const refreshHint = document.querySelector('#backup-refresh-hint');
const previousEl = document.querySelector('#backup-previous');
const initialPrevious = previousEl ? previousEl.textContent : '';
const fields = ['state', 'previous', 'upcoming', 'log'];

async function refreshBackupStatus() {
  if (document.hidden) return;
  const needLogs = Boolean(logPre && (!logPanel || logPanel.open));
  const path = needLogs ? '/backup/status?logs=1' : '/backup/status';
  try {
    const response = await fetch(path);
    if (!response.ok) return;
    const status = await response.json();
    if (statusBox && status.tone) {
      statusBox.dataset.status = status.tone;
    }
    for (const key of fields) {
      const field = document.querySelector(`#backup-${key}`);
      if (field && key in status && status[key] !== field.textContent) {
        const atEnd = field.scrollHeight - field.scrollTop - field.clientHeight < 12;
        field.textContent = status[key];
        if (key === 'log' && atEnd) field.scrollTop = field.scrollHeight;
      }
    }
    if (refreshHint && status.previous && status.previous !== initialPrevious) {
      refreshHint.hidden = false;
    }
  } catch {
    // Keep the last status available while the server is restarting.
  }
}

if (logPanel) {
  logPanel.addEventListener('toggle', () => {
    if (logPanel.open) {
      if (logPre) logPre.scrollTop = logPre.scrollHeight;
      refreshBackupStatus();
    }
  });
} else if (logPre) {
  logPre.scrollTop = logPre.scrollHeight;
}
window.addEventListener('pageshow', event => {
  if (event.persisted) window.location.reload();
});
window.setInterval(refreshBackupStatus, 4000);
