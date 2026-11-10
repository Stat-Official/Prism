/** Keep all collected articles visible and require source evidence for comparisons. */
export function findSourceMentions(text, sources) {
  const variants = [];
  for (const release of sources) {
    const title = release.title || '';
    const short = title.replace(/\([^)]*\)/g, '').replace(/보도자료/g, '').replace(/결과\s*$/, '');
    for (const value of new Set([title, short])) {
      const characters = [...value].filter(character => !/[\s「」『』“”‘’"']/u.test(character));
      if (characters.length < 6) continue;
      const pattern = characters.map(character => character.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')).join('[\\s「」『』“”‘’"\']*');
      variants.push({ release, pattern, length: characters.length });
    }
  }
  if (!variants.length) return [];
  variants.sort((a, b) => b.length - a.length);
  const pattern = new RegExp(variants.map(variant => `(${variant.pattern})`).join('|'), 'gu');
  return [...String(text).matchAll(pattern)].map(match => ({
    start: match.index, end: match.index + match[0].length,
    release: variants[match.slice(1).findIndex(value => value !== undefined)].release,
  }));
}

export function createComparisonModel(data) {
  const releases = new Map((data.releases || []).map(release => [release.release_id, release]));
  const sourceIds = article => [...new Set(article?.matchedReleaseIds || [])].filter(id => releases.has(id));
  const filterSourceIds = article => [...new Set([...(article?.releaseIds || []), ...sourceIds(article)])].filter(id => releases.has(id));
  const articles = new Map((data.articles || []).map(article => [article.id, article]));
  const usedSources = new Set([...articles.values()].flatMap(filterSourceIds));
  const visibleReleases = [...releases.values()].filter(release => usedSources.has(release.release_id));
  const cache = new Map();
  function relationsFor(article, releaseId, kind) {
    if (!article || !articles.has(article.id) || !sourceIds(article).includes(releaseId) || !['related', 'opposed'].includes(kind)) return [];
    const key = `${article.id}:${releaseId}:${kind}`;
    if (cache.has(key)) return cache.get(key);
    const seen = new Set();
    const result = (article.relations?.[kind] || []).filter(item => {
      const other = articles.get(item.articleId);
      if (!other || other.id === article.id || seen.has(other.id) || !item.releaseIds?.includes(releaseId) || !sourceIds(other).includes(releaseId)) return false;
      seen.add(other.id);
      return true;
    });
    cache.set(key, result);
    return result;
  }
  function comparisonCount(article, releaseId) {
    return new Set(['related', 'opposed'].flatMap(kind => relationsFor(article, releaseId, kind).map(item => item.articleId))).size;
  }
  return { articles, releases, visibleReleases, sourceIds, filterSourceIds, relationsFor, comparisonCount,
    sourcesFor: article => sourceIds(article).map(id => releases.get(id)),
     searchSourcesFor: article => filterSourceIds(article).map(id => releases.get(id)),
    canCompare: (article, releaseId) => comparisonCount(article, releaseId) > 0 };
}
