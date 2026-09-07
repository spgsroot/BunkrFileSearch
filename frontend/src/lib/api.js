export const PER_PAGE = 20;

export async function getJSON(url) {
  const r = await fetch(url);
  const text = await r.text();
  let j;
  try {
    j = JSON.parse(text);
  } catch {
    throw new Error('HTTP ' + r.status + (text ? ' (' + text.slice(0, 80) + ')' : ''));
  }
  if (!r.ok) throw new Error('HTTP ' + r.status);
  if (j && j.detail) {
    throw new Error(typeof j.detail === 'string' ? j.detail : JSON.stringify(j.detail).slice(0, 160));
  }
  return j;
}

export function listAlbums(page, per = 30) {
  return getJSON(`/api/albums?page=${page}&per=${per}&indexed=true`);
}

export function searchFiles(params) {
  return getJSON('/api/search?' + params.toString());
}

export function searchAlbumTitles(q, per = 5) {
  return getJSON('/api/albums?q=' + encodeURIComponent(q) + '&per=' + per);
}

export function randomAlbum() {
  return getJSON('/api/albums/random');
}

export function getStats() {
  return getJSON('/api/stats');
}

export function fmtTs(iso) {
  if (!iso) return '-';
  const d = new Date(iso);
  return isNaN(d) ? iso : d.toLocaleString([], { dateStyle: 'medium', timeStyle: 'short' });
}

export function fmtSize(b) {
  if (!b) return '';
  const u = ['B', 'KB', 'MB', 'GB'];
  let i = 0;
  while (b >= 1024 && i < 3) {
    b /= 1024;
    i++;
  }
  return b.toFixed(b >= 10 || i === 0 ? 0 : 1) + ' ' + u[i];
}

export function fmtDate(s) {
  return (s || '').replace('T', ' ').slice(0, 16);
}

export function albumUrl(id) {
  return 'https://bunkr.cr/a/' + id;
}

export function letterOf(title) {
  return ((title || '?').trim().charAt(0) || '?').toUpperCase();
}
