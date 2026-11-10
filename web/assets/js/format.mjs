/** Shared formatting for text rendered by the news portal. */
export const escapeHtml = value => String(value ?? '').replace(/[&<>"']/g, character => ({
  '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
})[character]);

export const formatDate = value => (value || '').slice(0, 10).replaceAll('-', '.');

export function readable(value) {
  const text = String(value ?? '');
  let result = '', cursor = 0;
  for (const match of text.matchAll(/[+−△]?\d+(?:[,.]\d+)*/g)) {
    result += escapeHtml(text.slice(cursor, match.index));
    result += `<span class="num">${escapeHtml(match[0])}</span>`;
    cursor = match.index + match[0].length;
  }
  return result + escapeHtml(text.slice(cursor));
}
