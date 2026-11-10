/** Load a complete published snapshot; use curated examples on a fresh checkout. */
export async function loadPortal(fetcher = fetch) {
  const response = await fetcher('current.json', { cache: 'no-store' });
  if (response.status === 404) {
    const demo = await fetcher('demo.json', { cache: 'no-store' });
    if (!demo.ok) throw new Error('Comparison examples are unavailable.');
    const data = await demo.json();
    if (data.schemaVersion !== 2 || data.meta?.mode !== 'curated_demo' ||
        !Array.isArray(data.articles) || !Array.isArray(data.releases) ||
        !Array.isArray(data.rssTitles) || !Array.isArray(data.articleSelections?.comparisons)) {
      throw new Error('Unsupported comparison examples.');
    }
    return data;
  }
  if (!response.ok) throw new Error('Portal manifest is unavailable.');
  const manifest = await response.json();
  if (![1, 2].includes(manifest.schemaVersion) ||
      !/^snapshots\/news_[a-f0-9]{32}\.json$/.test(manifest.data)) {
    throw new Error('Unsupported portal manifest.');
  }
  const responseData = await fetcher(manifest.data);
  if (!responseData.ok) throw new Error('Portal data is unavailable.');
  const data = await responseData.json();
  if (data.schemaVersion !== manifest.schemaVersion ||
      data.meta?.collectionId !== manifest.collectionId ||
      !Array.isArray(data.articles) || !Array.isArray(data.rssTitles)) {
    throw new Error('Portal data does not match its manifest.');
  }
  return data;
}
