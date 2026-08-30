export function parseDatedSnapshot(file) {
  const match = file.match(/^(\d{4}-\d{2}-\d{2})_([A-Za-z0-9-]+)(?:_([A-Za-z0-9-]+))?\.json$/);
  if (!match) return null;

  const [, date, sourceOrSlug, explicitSlug] = match;
  return {
    date,
    source: explicitSlug ? sourceOrSlug : 'nvidia',
    slug: explicitSlug ?? sourceOrSlug,
    file,
  };
}
